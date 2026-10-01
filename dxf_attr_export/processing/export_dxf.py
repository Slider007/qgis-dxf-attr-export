import math
import os

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsDxfExport,
    QgsExpressionContextUtils,
    QgsFeatureRequest,
    QgsField,
    QgsGeometry,
    QgsLineString,
    QgsMapSettings,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterCrs,
    QgsProcessingParameterDxfLayers,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterScale,
    QgsReadWriteContext,
    QgsRectangle,
    QgsPointXY,
    QgsRenderContext,
    QgsUnitTypes,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QCoreApplication, QDate, QDateTime, QFile, QIODevice, QMetaType, QTime, Qt
from qgis.PyQt.QtXml import QDomDocument

from .. import dxf_blocks
from .params import add, is_temporary

KEY_FIELD = "__dxf_attr_key"
KEY_PREFIX = "QFX_"

SYMBOLOGY_MODES = [
    Qgis.FeatureSymbologyExport.NoSymbology,
    Qgis.FeatureSymbologyExport.PerFeature,
    Qgis.FeatureSymbologyExport.PerSymbolLayer,
]
# Заливки, которые QgsDxfExport в DXF не переносит вовсе (остаётся только контур).
# Штриховку линиями модуль рисует сам, об остальных предупреждает.
UNSUPPORTED_FILLS = {
    "GradientFill": "градиент",
    "ShapeburstFill": "заливка с размытием (shapeburst)",
    "SVGFill": "SVG-заливка",
    "RasterFill": "растровая заливка",
    "PointPatternFill": "точечный узор",
    "CentroidFill": "заливка по центроиду",
    "RandomMarkerFill": "случайные значки",
}
MAX_HATCH_LINES = 5000  # защита от заливки с крошечным шагом на огромной площади

# Коды единиц чертежа ($INSUNITS) для AutoCAD
INSUNITS = {
    Qgis.DistanceUnit.Meters: 6,
    Qgis.DistanceUnit.Kilometers: 7,
    Qgis.DistanceUnit.Feet: 2,
    Qgis.DistanceUnit.Centimeters: 5,
    Qgis.DistanceUnit.Millimeters: 4,
}


def attribute_text(value):
    """Значение поля QGIS → строка атрибута (NULL → пусто)."""
    # NULL: None в QGIS 4, пустой QVariant в QGIS 3; пустые даты тоже isNull()
    if value is None or (hasattr(value, "isNull") and value.isNull()):
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return "{:.15g}".format(value)
    if isinstance(value, (QDate, QDateTime, QTime)):
        return value.toString(Qt.DateFormat.ISODate)
    if isinstance(value, (bytes, bytearray)) or type(value).__name__ == "QByteArray":
        return ""
    return str(value)


def anchor_point(geom):
    """Точка вставки блока: сама точка, середина линии, точка внутри полигона."""
    if geom is None or geom.isNull() or geom.isEmpty():
        return None
    gtype = geom.type()
    if gtype == Qgis.GeometryType.Point:
        p = next(geom.vertices())
        return (p.x(), p.y())
    if gtype == Qgis.GeometryType.Line:
        parts = geom.asGeometryCollection() if geom.isMultipart() else [geom]
        longest = max(parts, key=lambda g: g.length())
        p = longest.interpolate(longest.length() / 2.0)
    elif gtype == Qgis.GeometryType.Polygon:
        p = geom.pointOnSurface()
    else:
        p = QgsGeometry()
    if p is None or p.isNull() or p.isEmpty():
        v = next(geom.vertices())
        return (v.x(), v.y())
    pt = p.asPoint()
    return (pt.x(), pt.y())


def mm_to_map_units(value, unit, crs, scale):
    """Размер символа в единицы карты: миллиметры и пункты — через масштаб."""
    if unit == Qgis.RenderUnit.MapUnits:
        return value
    mm = value
    if unit == Qgis.RenderUnit.Points:
        mm = value * 25.4 / 72.0
    elif unit == Qgis.RenderUnit.Inches:
        mm = value * 25.4
    elif unit == Qgis.RenderUnit.Pixels:
        mm = value * 25.4 / 96.0
    elif unit == Qgis.RenderUnit.MetersInMapUnits:
        return value * QgsUnitTypes.fromUnitToUnitFactor(Qgis.DistanceUnit.Meters, crs.mapUnits())
    return mm / 1000.0 * scale * QgsUnitTypes.fromUnitToUnitFactor(
        Qgis.DistanceUnit.Meters, crs.mapUnits())


def hatch_lines(geom, angle, spacing, offset=0.0):
    """Линии штриховки внутри объекта: угол — как в QGIS (градусы по часовой от севера)."""
    if spacing <= 0:
        return None
    box = geom.boundingBox()
    if box.isEmpty():
        return []
    # угол линий в математическом виде (против часовой от востока)
    a = math.radians(90.0 - angle)
    dx, dy = math.cos(a), math.sin(a)
    nx, ny = -dy, dx  # поперёк линий
    cx, cy = box.center().x(), box.center().y()
    reach = math.hypot(box.width(), box.height()) / 2.0 + spacing
    steps = int(reach / spacing) + 1
    if steps * 2 + 1 > MAX_HATCH_LINES:
        return None
    out = []
    for k in range(-steps, steps + 1):
        shift = k * spacing + offset
        x0, y0 = cx + nx * shift - dx * reach, cy + ny * shift - dy * reach
        x1, y1 = cx + nx * shift + dx * reach, cy + ny * shift + dy * reach
        line = QgsGeometry(QgsLineString([QgsPointXY(x0, y0), QgsPointXY(x1, y1)]))
        piece = geom.intersection(line)
        if piece.isNull() or piece.isEmpty():
            continue
        parts = piece.asGeometryCollection() if piece.isMultipart() else [piece]
        for part in parts:
            if part.type() != Qgis.GeometryType.Line:
                continue
            pts = [(p.x(), p.y()) for p in part.asPolyline()]
            if len(pts) > 1:
                out.append(pts)
    return out


def pattern_fills(symbol):
    """Штриховки линиями и не переносимые заливки символа: ([(угол, шаг, единица, смещение, цвет)], [типы])."""
    hatches, missing = [], []
    if symbol is None or symbol.type() != Qgis.SymbolType.Fill:
        return hatches, missing
    for i in range(symbol.symbolLayerCount()):
        sl = symbol.symbolLayer(i)
        if not sl.enabled():
            continue
        kind = sl.layerType()
        if kind == "LinePatternFill":
            color = sl.color()
            line = sl.subSymbol()
            if line is not None and line.color().isValid():
                color = line.color()
            hatches.append((sl.lineAngle(), sl.distance(), sl.distanceUnit(), sl.offset(),
                            (color.red(), color.green(), color.blue())))
        elif kind in UNSUPPORTED_FILLS:
            missing.append(UNSUPPORTED_FILLS[kind])
    return hatches, missing


def polygon_rings(geom):
    """Контуры полигона: [(точки, внешний ли контур)] — для границ и штриховки."""
    g = QgsGeometry(geom)
    if QgsWkbTypes.isCurvedType(g.wkbType()):
        g.convertToStraightSegment()
    if g.type() != Qgis.GeometryType.Polygon:
        return []
    rings = []
    for poly in (g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]):
        for n, ring in enumerate(poly):
            pts = [(p.x(), p.y()) for p in ring]
            if len(pts) > 1 and pts[0] == pts[-1]:
                pts.pop()
            if len(pts) > 2:
                rings.append((pts, n == 0))
    return rings


def outline_shapes(geom):
    """Границы объекта для режима «только границы»: точки, полилинии, контуры полигонов."""
    g = QgsGeometry(geom)
    if QgsWkbTypes.isCurvedType(g.wkbType()):
        g.convertToStraightSegment()
    multi = g.isMultipart()
    gtype = g.type()
    if gtype == Qgis.GeometryType.Point:
        return [("POINT", [(p.x(), p.y())], False) for p in (g.asMultiPoint() if multi else [g.asPoint()])]
    if gtype == Qgis.GeometryType.Line:
        return [("LINE", [(p.x(), p.y()) for p in line], False)
                for line in (g.asMultiPolyline() if multi else [g.asPolyline()]) if len(line) > 1]
    if gtype == Qgis.GeometryType.Polygon:
        shapes = []
        for poly in (g.asMultiPolygon() if multi else [g.asPolygon()]):
            for ring in poly:
                pts = [(p.x(), p.y()) for p in ring]
                if len(pts) > 1 and pts[0] == pts[-1]:
                    pts.pop()
                if len(pts) > 1:
                    shapes.append(("LINE", pts, True))
        return shapes
    return []


def outline_color(symbol):
    """Цвет границы: обводка у полигонов (без обводки — заливка), цвет символа у линий и точек."""
    if symbol is None:
        return None
    color = symbol.color()
    if symbol.type() == Qgis.SymbolType.Fill:
        for i in range(symbol.symbolLayerCount()):
            sl = symbol.symbolLayer(i)
            if not sl.enabled():
                continue
            stroke = sl.strokeColor()
            no_pen = hasattr(sl, "strokeStyle") and sl.strokeStyle() == Qt.PenStyle.NoPen
            if stroke.isValid() and stroke.alpha() > 0 and not no_pen:
                color = stroke
            else:
                color = sl.color()
            break
    if not color.isValid():
        return None
    return (color.red(), color.green(), color.blue())


class ExportDxfAlgorithm(QgsProcessingAlgorithm):
    LAYERS = "LAYERS"
    BLOCK_CONTENT = "BLOCK_CONTENT"
    PATTERN_FILLS = "PATTERN_FILLS"
    HATCH_LINES, HATCH_NATIVE, HATCH_NONE = 0, 1, 2
    SYMBOLOGY_MODE = "SYMBOLOGY_MODE"
    SYMBOLOGY_SCALE = "SYMBOLOGY_SCALE"
    ENCODING = "ENCODING"
    CRS = "CRS"
    SELECTED_FEATURES_ONLY = "SELECTED_FEATURES_ONLY"
    USE_LAYER_TITLE = "USE_LAYER_TITLE"
    FORCE_2D = "FORCE_2D"
    MTEXT = "MTEXT"
    ATTRIBUTES_VISIBLE = "ATTRIBUTES_VISIBLE"
    WRITE_PRJ = "WRITE_PRJ"
    OUTPUT = "OUTPUT"

    def tr(self, text):
        return QCoreApplication.translate("ExportDxfAlgorithm", text)

    def createInstance(self):
        return ExportDxfAlgorithm()

    def name(self):
        return "exportdxf"

    def displayName(self):
        return self.tr("Экспорт в DXF с атрибутами")

    def group(self):
        return self.tr("Экспорт")

    def groupId(self):
        return "export"

    def shortHelpString(self):
        return self.tr(
            "Выгружает слои в DXF для AutoCAD. Сохраняются стили и подписи QGIS, "
            "а каждый объект записывается блоком с атрибутами: значения всех полей "
            "видны в «Свойствах» AutoCAD и извлекаются командой ИЗВЛЕЧЬДАННЫЕ "
            "(DATAEXTRACTION).\n\n"
            "Штриховку линиями QGIS в DXF не переносит. Модуль переносит её сам: "
            "линиями внутри контура объекта или штриховкой AutoCAD (одним объектом на "
            "контур — файл получается намного меньше).\n\n"
            "Оформление в блоках: полное (как на карте QGIS: заливки, значки, толщины) "
            "или только границы — полигоны замкнутыми полилиниями по каждому контуру, "
            "линии полилиниями, точки точками AutoCAD, цветом обводки символа.\n\n"
            "Слой AutoCAD — имя слоя QGIS или значение поля, выбранного в столбце "
            "«Output layer attribute» списка слоёв.\n\n"
            "Координаты пишутся в выбранной системе координат без смещений. Рядом с "
            "DXF создаётся файл .prj с описанием системы координат.\n\n"
            "Длинные значения (больше 250 символов) укорачиваются, о них выводится "
            "предупреждение.")

    def initAlgorithm(self, config=None):
        add(self, QgsProcessingParameterDxfLayers(self.LAYERS, self.tr("Слои")),
            self.tr("В столбце «Output layer attribute» можно выбрать поле, значение "
                    "которого станет именем слоя AutoCAD, — например тип опоры."))
        add(self, QgsProcessingParameterEnum(
            self.BLOCK_CONTENT, self.tr("Оформление в блоках"),
            [self.tr("Полное оформление QGIS"),
             self.tr("Только границы объектов")],
            defaultValue=0),
            self.tr("«Полное оформление» — цвета, толщины, заливки и значки, как на карте. "
                    "«Только границы» — полигоны замкнутыми полилиниями (с дырками), линии "
                    "полилиниями, точки точками AutoCAD."))
        add(self, QgsProcessingParameterEnum(
            self.PATTERN_FILLS, self.tr("Штриховка линиями"),
            [self.tr("Линиями — файл крупнее"),
             self.tr("Штриховкой AutoCAD — компактнее"),
             self.tr("Не переносить")],
            defaultValue=0),
            self.tr("Штриховку линиями встроенный экспорт QGIS не переносит вовсе, модуль "
                    "рисует её сам. Линиями — надёжно, но файл в несколько раз крупнее; "
                    "штриховкой AutoCAD — один объект на контур."))
        add(self, QgsProcessingParameterCrs(
            self.CRS, self.tr("Система координат"), defaultValue="ProjectCrs"),
            self.tr("Координаты пишутся в неё без смещений; рядом с DXF кладётся файл .prj."))
        add(self, QgsProcessingParameterBoolean(
            self.SELECTED_FEATURES_ONLY, self.tr("Только выделенные объекты"),
            defaultValue=False),
            self.tr("Выгрузится только то, что выделено на карте. Если не выделено ничего, "
                    "модуль об этом скажет."))
        add(self, QgsProcessingParameterEnum(
            self.SYMBOLOGY_MODE, self.tr("Перенос стилей"),
            [self.tr("Без стилей"), self.tr("Стили объектов"), self.tr("Стили слоёв символов")],
            defaultValue=1),
            self.tr("«Стили объектов» подходит почти всегда. «Стили слоёв символов» дробит "
                    "сложные символы на части — файл крупнее."), advanced=True)
        add(self, QgsProcessingParameterScale(
            self.SYMBOLOGY_SCALE, self.tr("Масштаб для стилей и подписей"), defaultValue=1000),
            self.tr("От него зависят толщины линий, шаг штриховки и размер подписей в "
                    "чертеже: 1:1000 — как выглядит карта в этом масштабе."), advanced=True)
        encodings = QgsDxfExport.encodings()
        # в QGIS 4 список записан в нижнем регистре («cp1251»), поэтому без учёта регистра
        default = next((i for i, e in enumerate(encodings) if e.lower() == "cp1251"), 0)
        add(self, QgsProcessingParameterEnum(
            self.ENCODING, self.tr("Кодировка"), encodings, defaultValue=default),
            self.tr("CP1251 — обычная кодировка русских чертежей AutoCAD."), advanced=True)
        add(self, QgsProcessingParameterBoolean(
            self.USE_LAYER_TITLE, self.tr("Имя слоя AutoCAD — заголовок слоя QGIS, а не имя"),
            defaultValue=False),
            self.tr("Заголовок задаётся в свойствах слоя, на вкладке «Информация о QGIS "
                    "сервере»."), advanced=True)
        add(self, QgsProcessingParameterBoolean(
            self.FORCE_2D, self.tr("Только 2D (без высот Z)"), defaultValue=False),
            advanced=True)
        add(self, QgsProcessingParameterBoolean(
            self.MTEXT, self.tr("Подписи многострочным текстом (MTEXT)"), defaultValue=True),
            self.tr("Если AutoCAD показывает подписи не так, как ожидалось, попробуйте "
                    "выключить — подписи станут обычным текстом (TEXT)."), advanced=True)
        add(self, QgsProcessingParameterBoolean(
            self.ATTRIBUTES_VISIBLE, self.tr("Показывать атрибуты на чертеже"),
            defaultValue=False),
            self.tr("По умолчанию атрибуты скрыты: они видны в «Свойствах» объекта и "
                    "извлекаются командой ИЗВЛЕЧЬДАННЫЕ, но не загромождают чертёж."),
            advanced=True)
        add(self, QgsProcessingParameterBoolean(
            self.WRITE_PRJ, self.tr("Записать файл .prj с системой координат"),
            defaultValue=True), advanced=True)
        add(self, QgsProcessingParameterFileDestination(
            self.OUTPUT, self.tr("Файл DXF"), self.tr("Файлы DXF (*.dxf)")),
            self.tr("Открывается в AutoCAD как есть; рядом кладётся файл .prj с системой "
                    "координат. Формат DXF 2000, при необходимости сохраните его в AutoCAD "
                    "как DWG."))

    def prepareAlgorithm(self, parameters, context, feedback):
        """Копии слоёв готовятся в основном потоке: слои проекта читаются здесь."""
        dxf_layers = QgsProcessingParameterDxfLayers.parameterAsLayers(parameters[self.LAYERS], context)
        if not dxf_layers:
            raise QgsProcessingException(self.tr("Выберите хотя бы один слой в списке «Слои»"))
        selected_only = self.parameterAsBoolean(parameters, self.SELECTED_FEATURES_ONLY, context)
        use_title = self.parameterAsBoolean(parameters, self.USE_LAYER_TITLE, context)
        self._jobs = []
        counter = 0
        for dl in dxf_layers:
            layer = dl.layer()
            if layer is None or not layer.isValid():
                raise QgsProcessingException(self.tr("Один из выбранных слоёв не открывается. Проверьте, что файл на месте и не занят другой программой, затем выберите слои заново"))
            request = QgsFeatureRequest()
            if selected_only:
                request.setFilterFids(layer.selectedFeatureIds())
            copy = layer.materialize(request)
            copy.setName(layer.name())
            style = QDomDocument()
            layer.exportNamedStyle(style, QgsReadWriteContext())
            copy.importNamedStyle(style)

            fields = layer.fields()
            if fields.lookupField(KEY_FIELD) >= 0:
                raise QgsProcessingException(
                    self.tr("В слое «{}» уже есть поле {} — модуль занимает его под служебное. Переименуйте поле в слое и повторите").format(layer.name(), KEY_FIELD))
            copy.dataProvider().addAttributes([QgsField(KEY_FIELD, QMetaType.Type.QString)])
            copy.updateFields()
            key_idx = copy.fields().lookupField(KEY_FIELD)

            title = layer.serverProperties().title()
            default_name = dl.overriddenName() or (title if use_title and title else layer.name())
            name_idx = dl.layerOutputAttributeIndex()
            features, changes = [], {}
            for f in copy.getFeatures():
                counter += 1
                key = "{}{}".format(KEY_PREFIX, counter)
                changes[f.id()] = {key_idx: key}
                target = default_name
                if 0 <= name_idx < fields.count():
                    v = attribute_text(f.attribute(name_idx)).strip()
                    if v:
                        target = v
                values = [attribute_text(f.attribute(i)) for i in range(fields.count())]
                features.append((f.id(), dxf_blocks.FeatureBlock(
                    key, dxf_blocks.safe_name(target), None, values)))
            copy.dataProvider().changeAttributeValues(changes)
            prompts = [(fld.name(), fld.displayName()) for fld in fields]
            self._jobs.append((copy, dl, key_idx, prompts, features))
            if selected_only:
                feedback.pushInfo(self.tr("Слой «{}»: выделено объектов — {}").format(
                    layer.name(), len(features)))
            else:
                feedback.pushInfo(self.tr("Слой «{}»: объектов — {}").format(layer.name(), len(features)))
        if selected_only and not any(job[4] for job in self._jobs):
            raise QgsProcessingException(self.tr(
                "Включено «Только выделенные объекты», но в выбранных слоях ничего не выделено. "
                "Выделите объекты на карте или снимите эту галочку."))
        return True

    def processAlgorithm(self, parameters, context, feedback):
        crs = self.parameterAsCrs(parameters, self.CRS, context)
        if not crs.isValid():
            raise QgsProcessingException(self.tr("Выберите систему координат: в ней будут записаны координаты чертежа"))
        mode = SYMBOLOGY_MODES[self.parameterAsEnum(parameters, self.SYMBOLOGY_MODE, context)]
        scale = self.parameterAsDouble(parameters, self.SYMBOLOGY_SCALE, context) or 1000.0
        encoding = QgsDxfExport.encodings()[self.parameterAsEnum(parameters, self.ENCODING, context)]
        force_2d = self.parameterAsBoolean(parameters, self.FORCE_2D, context)
        mtext = self.parameterAsBoolean(parameters, self.MTEXT, context)
        visible = self.parameterAsBoolean(parameters, self.ATTRIBUTES_VISIBLE, context)
        outlines = self.parameterAsEnum(parameters, self.BLOCK_CONTENT, context) == 1
        hatch_mode = self.parameterAsEnum(parameters, self.PATTERN_FILLS, context)
        do_hatch = hatch_mode != self.HATCH_NONE and not outlines
        missing_fills = {}
        too_dense = 0
        write_prj = self.parameterAsBoolean(parameters, self.WRITE_PRJ, context)
        path = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        if not path:
            raise QgsProcessingException(self.tr("Укажите, куда сохранить файл DXF"))

        # Точки вставки блоков и общий охват — в выбранной СК
        total = sum(len(job[4]) for job in self._jobs) or 1
        done = 0
        extent = QgsRectangle()
        export_layers, blocks_layers = [], []
        for copy, dl, key_idx, prompts, features in self._jobs:
            transform = QgsCoordinateTransform(copy.crs(), crs, context.transformContext())
            feats = {f.id(): f for f in copy.getFeatures()}
            renderer = None
            if (outlines or do_hatch) and copy.renderer() is not None:
                rctx = QgsRenderContext()
                rctx.setRendererScale(scale)
                rctx.expressionContext().appendScopes(
                    QgsExpressionContextUtils.globalProjectLayerScopes(copy))
                renderer = copy.renderer().clone()
                renderer.startRender(rctx, copy.fields())
            for fid, fb in features:
                if feedback.isCanceled():
                    return {}
                feat = feats.get(fid)
                geom = QgsGeometry(feat.geometry() if feat is not None else QgsGeometry())
                if not geom.isNull() and not geom.isEmpty():
                    try:
                        geom.transform(transform)
                    except Exception as e:  # точка вне области действия СК
                        raise QgsProcessingException(
                            self.tr("Объект слоя «{}» не пересчитывается в {} — возможно, он лежит вне области действия этой системы координат. Выберите подходящую СК. Подробности: {}").format(
                                copy.name(), crs.authid(), e))
                    fb.anchor = anchor_point(geom)
                    extent.combineExtentWith(geom.boundingBox())
                    symbol = None
                    if renderer is not None:
                        rctx.expressionContext().setFeature(feat)
                        if renderer.willRenderFeature(feat, rctx):
                            symbol = renderer.symbolForFeature(feat, rctx)
                    if outlines:
                        fb.shapes = outline_shapes(geom) if symbol is not None else []
                        fb.color = outline_color(symbol)
                    elif do_hatch and symbol is not None:
                        hatches, missing = pattern_fills(symbol)
                        for name in missing:
                            missing_fills[name] = missing_fills.get(name, 0) + 1
                        for angle, distance, unit, shift, color in hatches:
                            spacing = mm_to_map_units(distance, unit, crs, scale)
                            if spacing <= 0:
                                continue
                            if hatch_mode == self.HATCH_NATIVE:
                                rings = polygon_rings(geom)
                                if rings:
                                    fb.hatches.append(
                                        (color, (90.0 - angle) % 180.0, spacing, rings))
                                continue
                            lines = hatch_lines(geom, angle, spacing,
                                                mm_to_map_units(shift, unit, crs, scale))
                            if lines is None:
                                too_dense += 1
                                continue
                            fb.extra += [("LINE", pts, False, color) for pts in lines]
                done += 1
                feedback.setProgress(20.0 * done / total)
            if renderer is not None:
                renderer.stopRender(rctx)
            export_layers.append(QgsDxfExport.DxfLayer(
                copy, key_idx, dl.buildDataDefinedBlocks(),
                dl.dataDefinedBlocksMaximumNumberOfClasses(), dl.overriddenName()))
            blocks_layers.append(dxf_blocks.ExportLayer(prompts, [fb for _, fb in features]))
        if extent.isNull():
            raise QgsProcessingException(self.tr("В выбранных слоях нет объектов с геометрией — выгружать нечего. Проверьте, что в слоях есть объекты и что они не скрыты стилем"))
        pad = max(extent.width(), extent.height(), 1.0) * 0.01
        extent.grow(pad)

        feedback.pushInfo(self.tr("Запись DXF…"))
        settings = QgsMapSettings()
        settings.setTransformContext(context.transformContext())
        settings.setDestinationCrs(crs)
        settings.setLayers([job[0] for job in self._jobs])
        settings.setExtent(extent)
        export = QgsDxfExport()
        export.setMapSettings(settings)
        export.addLayers(export_layers)
        export.setSymbologyScale(scale)
        export.setSymbologyExport(mode)
        export.setDestinationCrs(crs)
        export.setForce2d(force_2d)
        export.setExtent(extent)
        if not mtext:
            export.setFlags(QgsDxfExport.Flag.FlagNoMText)
        out = QFile(path)
        if not out.open(QIODevice.OpenModeFlag.WriteOnly | QIODevice.OpenModeFlag.Truncate):
            raise QgsProcessingException(self.tr("Не удалось сохранить «{}». Возможно, файл открыт в другой программе — закройте её и повторите").format(path))
        status = export.writeToFile(out, encoding)
        out.close()
        if status != QgsDxfExport.ExportResult.Success:
            raise QgsProcessingException(self.tr("QGIS не смог записать DXF: {} {}. Попробуйте другую папку или уберите из выгрузки слой, на котором всё остановилось").format(
                status, export.feedbackMessage()))
        feedback.setProgress(60)
        if feedback.isCanceled():
            return {}

        feedback.pushInfo(self.tr("Запись атрибутов в блоки…"))
        factor = QgsUnitTypes.fromUnitToUnitFactor(Qgis.DistanceUnit.Meters, crs.mapUnits())
        text_height = 0.0025 * scale * factor
        try:
            result = dxf_blocks.add_attribute_blocks(
                path, encoding, blocks_layers, text_height=text_height, visible=visible,
                insunits=INSUNITS.get(crs.mapUnits()))
        except ValueError as e:
            # разбор DXF, записанного QGIS: до человека это должно дойти словами,
            # а не трассировкой
            raise QgsProcessingException(self.tr(
                "Не удалось записать атрибуты в блоки: {}. Файл DXF остался без атрибутов — "
                "попробуйте выгрузить меньше слоёв или выберите другую кодировку.").format(e))
        feedback.setProgress(95)

        if write_prj:
            prj = os.path.splitext(path)[0] + ".prj"
            with open(prj, "w", encoding="utf-8") as f:
                f.write(crs.toWkt(Qgis.CrsWktVariant.Wkt1Esri))

        feedback.pushInfo(self.tr("Объектов записано блоками с атрибутами: {}").format(result.blocks))
        for name, count in sorted(missing_fills.items()):
            feedback.reportError(self.tr(
                "Заливка «{}» в DXF не переносится (объектов: {}) — остаётся только контур").format(
                    name, count))
        if too_dense:
            feedback.reportError(self.tr(
                "У {} объектов шаг штриховки слишком мелкий для их площади — штриховка пропущена").format(
                    too_dense))
        if result.without_geometry:
            feedback.reportError(self.tr(
                "Объектов без геометрии или не попавших в DXF: {} — они не выгружены").format(
                    result.without_geometry))
        if result.truncated:
            feedback.reportError(self.tr(
                "Значений длиннее {} символов укорочено: {}").format(
                    dxf_blocks.MAX_VALUE_LEN, result.truncated))
        if is_temporary(path):
            feedback.pushWarning(self.tr(
                "Файл записан во временную папку и пропадёт при очистке временных файлов. "
                "Чтобы отдать чертёж в работу, запустите снова, указав постоянный путь."))
        feedback.setProgress(100)
        self._jobs = []
        return {self.OUTPUT: path}
