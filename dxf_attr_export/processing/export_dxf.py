import os

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsDxfExport,
    QgsExpressionContextUtils,
    QgsFeatureRequest,
    QgsField,
    QgsGeometry,
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
    QgsRenderContext,
    QgsUnitTypes,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QCoreApplication, QDate, QDateTime, QFile, QIODevice, QMetaType, QTime, Qt
from qgis.PyQt.QtXml import QDomDocument

from .. import dxf_blocks

KEY_FIELD = "__dxf_attr_key"
KEY_PREFIX = "QFX_"

SYMBOLOGY_MODES = [
    Qgis.FeatureSymbologyExport.NoSymbology,
    Qgis.FeatureSymbologyExport.PerFeature,
    Qgis.FeatureSymbologyExport.PerSymbolLayer,
]
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
        self.addParameter(QgsProcessingParameterDxfLayers(self.LAYERS, self.tr("Слои")))
        self.addParameter(QgsProcessingParameterEnum(
            self.BLOCK_CONTENT, self.tr("Оформление в блоках"),
            [self.tr("Полное оформление QGIS (заливки, значки, толщины)"),
             self.tr("Только границы объектов (полилинии и точки)")],
            defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            self.SYMBOLOGY_MODE, self.tr("Перенос стилей"),
            [self.tr("Без стилей"), self.tr("Стили объектов"), self.tr("Стили слоёв символов")],
            defaultValue=1))
        self.addParameter(QgsProcessingParameterScale(
            self.SYMBOLOGY_SCALE, self.tr("Масштаб для стилей и подписей"), defaultValue=1000))
        encodings = QgsDxfExport.encodings()
        self.addParameter(QgsProcessingParameterEnum(
            self.ENCODING, self.tr("Кодировка"), encodings,
            defaultValue=encodings.index("CP1251") if "CP1251" in encodings else 0))
        self.addParameter(QgsProcessingParameterCrs(
            self.CRS, self.tr("Система координат"), defaultValue="ProjectCrs"))
        self.addParameter(QgsProcessingParameterBoolean(
            self.SELECTED_FEATURES_ONLY, self.tr("Только выделенные объекты"), defaultValue=False))
        self.addParameter(QgsProcessingParameterBoolean(
            self.USE_LAYER_TITLE, self.tr("Имя слоя AutoCAD — заголовок слоя QGIS, а не имя"),
            defaultValue=False))
        self.addParameter(QgsProcessingParameterBoolean(
            self.FORCE_2D, self.tr("Только 2D (без высот Z)"), defaultValue=False))
        self.addParameter(QgsProcessingParameterBoolean(
            self.MTEXT, self.tr("Подписи многострочным текстом (MTEXT)"), defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.ATTRIBUTES_VISIBLE, self.tr("Показывать атрибуты на чертеже"), defaultValue=False))
        self.addParameter(QgsProcessingParameterBoolean(
            self.WRITE_PRJ, self.tr("Записать файл .prj с системой координат"), defaultValue=True))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT, self.tr("Файл DXF"), self.tr("Файлы DXF (*.dxf)")))

    def prepareAlgorithm(self, parameters, context, feedback):
        """Копии слоёв готовятся в основном потоке: слои проекта читаются здесь."""
        dxf_layers = QgsProcessingParameterDxfLayers.parameterAsLayers(parameters[self.LAYERS], context)
        if not dxf_layers:
            raise QgsProcessingException(self.tr("Не выбран ни один слой"))
        selected_only = self.parameterAsBoolean(parameters, self.SELECTED_FEATURES_ONLY, context)
        use_title = self.parameterAsBoolean(parameters, self.USE_LAYER_TITLE, context)
        self._jobs = []
        counter = 0
        for dl in dxf_layers:
            layer = dl.layer()
            if layer is None or not layer.isValid():
                raise QgsProcessingException(self.tr("Слой недоступен"))
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
                    self.tr("В слое «{}» уже есть поле {}").format(layer.name(), KEY_FIELD))
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
            raise QgsProcessingException(self.tr("Не задана система координат"))
        mode = SYMBOLOGY_MODES[self.parameterAsEnum(parameters, self.SYMBOLOGY_MODE, context)]
        scale = self.parameterAsDouble(parameters, self.SYMBOLOGY_SCALE, context) or 1000.0
        encoding = QgsDxfExport.encodings()[self.parameterAsEnum(parameters, self.ENCODING, context)]
        force_2d = self.parameterAsBoolean(parameters, self.FORCE_2D, context)
        mtext = self.parameterAsBoolean(parameters, self.MTEXT, context)
        visible = self.parameterAsBoolean(parameters, self.ATTRIBUTES_VISIBLE, context)
        outlines = self.parameterAsEnum(parameters, self.BLOCK_CONTENT, context) == 1
        write_prj = self.parameterAsBoolean(parameters, self.WRITE_PRJ, context)
        path = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        if not path:
            raise QgsProcessingException(self.tr("Не задан файл DXF"))

        # Точки вставки блоков и общий охват — в выбранной СК
        total = sum(len(job[4]) for job in self._jobs) or 1
        done = 0
        extent = QgsRectangle()
        export_layers, blocks_layers = [], []
        for copy, dl, key_idx, prompts, features in self._jobs:
            transform = QgsCoordinateTransform(copy.crs(), crs, context.transformContext())
            feats = {f.id(): f for f in copy.getFeatures()}
            renderer = None
            if outlines and copy.renderer() is not None:
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
                            self.tr("Не удалось пересчитать объект слоя «{}» в {}: {}").format(
                                copy.name(), crs.authid(), e))
                    fb.anchor = anchor_point(geom)
                    extent.combineExtentWith(geom.boundingBox())
                    if outlines:
                        fb.shapes, fb.color = outline_shapes(geom), None
                        if renderer is not None:
                            rctx.expressionContext().setFeature(feat)
                            if renderer.willRenderFeature(feat, rctx):
                                fb.color = outline_color(renderer.symbolForFeature(feat, rctx))
                            else:  # скрыт стилем слоя, как и в полном оформлении
                                fb.shapes = []
                done += 1
                feedback.setProgress(20.0 * done / total)
            if renderer is not None:
                renderer.stopRender(rctx)
            export_layers.append(QgsDxfExport.DxfLayer(
                copy, key_idx, dl.buildDataDefinedBlocks(),
                dl.dataDefinedBlocksMaximumNumberOfClasses(), dl.overriddenName()))
            blocks_layers.append(dxf_blocks.ExportLayer(prompts, [fb for _, fb in features]))
        if extent.isNull():
            raise QgsProcessingException(self.tr("В выбранных слоях нет объектов с геометрией"))
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
            raise QgsProcessingException(self.tr("Не удалось открыть файл для записи: {}").format(path))
        status = export.writeToFile(out, encoding)
        out.close()
        if status != QgsDxfExport.ExportResult.Success:
            raise QgsProcessingException(self.tr("QGIS не смог записать DXF: {} {}").format(
                status, export.feedbackMessage()))
        feedback.setProgress(60)
        if feedback.isCanceled():
            return {}

        feedback.pushInfo(self.tr("Запись атрибутов в блоки…"))
        factor = QgsUnitTypes.fromUnitToUnitFactor(Qgis.DistanceUnit.Meters, crs.mapUnits())
        text_height = 0.0025 * scale * factor
        result = dxf_blocks.add_attribute_blocks(
            path, encoding, blocks_layers, text_height=text_height, visible=visible,
            insunits=INSUNITS.get(crs.mapUnits()))
        feedback.setProgress(95)

        if write_prj:
            prj = os.path.splitext(path)[0] + ".prj"
            with open(prj, "w", encoding="utf-8") as f:
                f.write(crs.toWkt(Qgis.CrsWktVariant.Wkt1Esri))

        feedback.pushInfo(self.tr("Объектов записано блоками с атрибутами: {}").format(result.blocks))
        if result.without_geometry:
            feedback.reportError(self.tr(
                "Объектов без геометрии или не попавших в DXF: {} — они не выгружены").format(
                    result.without_geometry))
        if result.truncated:
            feedback.reportError(self.tr(
                "Значений длиннее {} символов укорочено: {}").format(
                    dxf_blocks.MAX_VALUE_LEN, result.truncated))
        feedback.setProgress(100)
        self._jobs = []
        return {self.OUTPUT: path}
