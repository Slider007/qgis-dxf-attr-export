"""Импорт DXF: чертёж AutoCAD → слои ГИС в GeoPackage.

Чем это отличается от простого открытия DXF в QGIS:

- объекты раскладываются по слоям чертежа и видам геометрии отдельными слоями,
  а не сваливаются в один слой «entities» со смешанной геометрией;
- задаётся система координат — в самом DXF её нет;
- замкнутые полилинии читаются полигонами, а не линиями;
- атрибуты блоков (ATTRIB) попадают в поля таблицы: GDAL отдаёт их отдельными
  подписями и теряет имена, поэтому они читаются из файла (`dxf_read`) и
  возвращаются объектам по дескриптору вставки (EntityHandle);
- подписи (TEXT, MTEXT) привязываются к полигонам, внутрь которых попали;
- кодировка определяется по содержимому файла: конвертеры DWG→DXF часто пишут
  UTF-8, объявляя в заголовке CP1251, и тогда имена слоёв выходят кракозябрами.

Читает и пишет GDAL, причём пишет одним соединением: несколько одновременно
открытых писателей в один GeoPackage дают «database is locked».
"""

import os

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterCrs,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFile,
    QgsProcessingParameterFileDestination,
    QgsRectangle,
    QgsSpatialIndex,
)
from qgis.PyQt.QtCore import QCoreApplication

from .. import dxf_read
from .params import add, is_temporary

# Поля, которые получает каждый слой. Имена латиницей — как и в остальных модулях.
BASE_FIELDS = ["dxf_layer", "entity", "handle", "block", "linetype", "text"]
MAX_FIELD_LEN = 60
LABEL_JOIN = "; "  # подписи, попавшие в один полигон, склеиваются этим
# Больше подписей в объекте — это не его подпись, а чужой текст внутри контура
# (съёмочные точки внутри большой зоны): такие остаются отдельным слоем.
MAX_BOUND_LABELS = 3

LABEL_MODES = [
    "Привязать к полигонам",
    "Отдельным слоем точек",
    "Не загружать",
]
BIND_LABELS, LABELS_APART, NO_LABELS = 0, 1, 2
BLOCK_MODES = [
    "Одним объектом",
    "Разобрать на фигуры",
]
ENCODINGS = ["Определить по файлу", "CP1251", "UTF-8", "CP866", "KOI8-R"]
AUTO_ENCODING = 0

# Справочники СК, коды которых понимает GDAL. Пользовательская СК QGIS
# («USER:100000», такой бывает МСК) среди них нет: её GDAL принимает только описанием.
AUTHORITIES = ("EPSG:", "ESRI:", "IGNF:", "OGC:", "IAU_2015:")

# Бесплатный переводчик DWG → DXF (GNU, годится и для коммерческой работы)
LIBREDWG_SITE = "https://www.gnu.org/software/libredwg/"

# Виды геометрии: они же суффиксы в именах слоёв
POINT, LINE, POLYGON = "точки", "линии", "полигоны"


def ogr_types(ogr):
    """Вид геометрии → типы OGR: (плоский, с высотой)."""
    return {
        POINT: (ogr.wkbMultiPoint, ogr.wkbMultiPoint25D),
        LINE: (ogr.wkbMultiLineString, ogr.wkbMultiLineString25D),
        POLYGON: (ogr.wkbMultiPolygon, ogr.wkbMultiPolygon25D),
    }


def geometry_kind(ogr, geom_type):
    """Тип геометрии OGR → вид: точки, линии или полигоны."""
    flat = ogr.GT_Flatten(geom_type)
    if flat in (ogr.wkbPoint, ogr.wkbMultiPoint):
        return POINT
    if flat in (ogr.wkbLineString, ogr.wkbMultiLineString, ogr.wkbCircularString,
                ogr.wkbCompoundCurve, ogr.wkbMultiCurve):
        return LINE
    if flat in (ogr.wkbPolygon, ogr.wkbMultiPolygon, ogr.wkbCurvePolygon,
                ogr.wkbMultiSurface):
        return POLYGON
    return None


def as_polygon(ogr, line):
    """Замкнутая линия → полигон; незамкнутая — None.

    Замыкание делается здесь, а не настройкой GDAL `DXF_CLOSED_LINE_AS_POLYGON`:
    её нет в GDAL до 3.4 (QGIS 3.40), и поведение разошлось бы по версиям.
    """
    if ogr.GT_Flatten(line.GetGeometryType()) == ogr.wkbMultiLineString:
        out = ogr.Geometry(ogr.wkbMultiPolygon)
        for i in range(line.GetGeometryCount()):
            part = as_polygon(ogr, line.GetGeometryRef(i))
            if part is None:
                return None
            out.AddGeometry(part)
        return out if out.GetGeometryCount() else None
    count = line.GetPointCount()
    if count < 4 or line.GetPoint_2D(0) != line.GetPoint_2D(count - 1):
        return None
    return ogr.ForceToPolygon(line.Clone())


def split_by_kind(ogr, geom, closed_as_polygon=False):
    """Геометрия → {вид: [фигуры]}. Коллекция (вставка блока) разбирается по видам.

    Одна вставка блока даёт не больше трёх объектов — по одному на вид
    геометрии, а не по одному на каждую фигуру внутри блока.
    """
    out = {}
    stack = [geom]
    while stack:
        g = stack.pop()
        if g is None or g.IsEmpty():
            continue
        if ogr.GT_Flatten(g.GetGeometryType()) == ogr.wkbGeometryCollection:
            stack += [g.GetGeometryRef(i) for i in range(g.GetGeometryCount())]
            continue
        kind = geometry_kind(ogr, g.GetGeometryType())
        if kind == LINE and closed_as_polygon:
            ring = as_polygon(ogr, g)
            if ring is not None:
                g, kind = ring, POLYGON
        if kind is not None:
            out.setdefault(kind, []).append(g)
    return out


def entity_name(subclasses):
    """«AcDbEntity:AcDbText:AcDbAttribute» → «ATTRIBUTE»."""
    last = (subclasses or "").split(":")[-1]
    if last.startswith("AcDb"):
        last = last[4:]
    return last.upper()


def is_attribute(subclasses, handle, attrib_handles):
    """Это значение атрибута блока, а не самостоятельная подпись?

    Дескриптор надёжнее подклассов: метки «100 AcDbAttribute» есть не во всех
    файлах (их не пишут некоторые конвертеры), а дескриптор есть всегда.
    """
    return handle in attrib_handles or (subclasses or "").endswith("AcDbAttribute")


def is_label(subclasses, handle, label_handles):
    """Это подпись (TEXT или MTEXT)?"""
    if handle in label_handles:
        return True
    sub = subclasses or ""
    return ("AcDbText" in sub or "AcDbMText" in sub) and not sub.endswith("AcDbAttribute")


def field_name(tag, taken):
    """Тег атрибута → имя поля: без пробелов, в нижнем регистре, без повторов."""
    name = "".join(c if (c.isalnum() or c == "_") else "_" for c in str(tag)).strip("_")
    name = (name[:MAX_FIELD_LEN] or "attr").lower()
    base, n = name, 2
    while name in taken:
        name = "{}_{}".format(base[:MAX_FIELD_LEN - 3], n)
        n += 1
    taken.add(name)
    return name


def layer_name(dxf_layer, kind):
    """Имя слоя в GeoPackage: «Границы зон (полигоны)».

    GeoPackage — это SQLite: имя таблицы начинается с буквы или подчёркивания,
    а в чертежах встречаются слои вида «!РКЗ_ВЛ 220 кВ».
    """
    base = "".join(" " if ord(c) < 32 else c for c in (dxf_layer or ""))
    base = base.encode("utf-8", "replace").decode("utf-8").strip() or "0"
    if not (base[0].isalpha() or base[0] == "_"):
        base = "_" + base
    return "{} ({})".format(base, kind)


def free_name(name, used):
    """Имя слоя, которого ещё нет: совпадения разводятся номером."""
    out, n = name, 2
    while out in used:
        out = "{} {}".format(name, n)
        n += 1
    used.add(out)
    return out


class Extent:
    """Охват чертежа: нужен, чтобы заметить несуразную систему координат."""

    def __init__(self):
        self.x_min = self.y_min = None
        self.x_max = self.y_max = None

    def add(self, box):
        x_min, x_max, y_min, y_max = box
        if self.x_min is None:
            self.x_min, self.x_max, self.y_min, self.y_max = x_min, x_max, y_min, y_max
            return
        self.x_min = min(self.x_min, x_min)
        self.x_max = max(self.x_max, x_max)
        self.y_min = min(self.y_min, y_min)
        self.y_max = max(self.y_max, y_max)

    def largest(self):
        """Наибольшая по модулю координата: (x, y)."""
        if self.x_min is None:
            return (0.0, 0.0)
        return (max(abs(self.x_min), abs(self.x_max)),
                max(abs(self.y_min), abs(self.y_max)))

    def beyond_degrees(self):
        """Координаты заведомо не градусы?"""
        if self.x_min is None:
            return False
        x, y = self.largest()
        return x > 180 or y > 90


class Group:
    """Будущий слой: имя, вид геометрии, поля, число объектов."""

    def __init__(self, name, kind, has_z=False):
        self.name = name
        self.kind = kind
        self.has_z = has_z  # хоть у одного объекта есть Z — слой объёмный
        self.tags = []  # теги атрибутов блоков, в порядке появления
        self.field_of = {}  # тег атрибута → имя поля
        self.fields = list(BASE_FIELDS)
        self.count = 0
        self.layer = None  # слой OGR на время записи

    def build_fields(self):
        taken = set(BASE_FIELDS)
        for tag in self.tags:
            name = field_name(tag, taken)
            self.field_of[tag] = name
            self.fields.append(name)


class ImportDxfAlgorithm(QgsProcessingAlgorithm):
    INPUT = "INPUT"
    CRS = "CRS"
    ENCODING = "ENCODING"
    CLOSED_AS_POLYGON = "CLOSED_AS_POLYGON"
    BLOCK_ATTRS = "BLOCK_ATTRS"
    BLOCKS = "BLOCKS"
    LABELS = "LABELS"
    SPLIT_BY_LAYER = "SPLIT_BY_LAYER"
    KEEP_Z = "KEEP_Z"
    OUTPUT = "OUTPUT"

    def tr(self, text):
        return QCoreApplication.translate("ImportDxfAlgorithm", text)

    def createInstance(self):
        return ImportDxfAlgorithm()

    def name(self):
        return "importdxf"

    def displayName(self):
        return self.tr("Импорт DXF в ГИС-формат")

    def group(self):
        return self.tr("Импорт")

    def groupId(self):
        return "import"

    def shortHelpString(self):
        return self.tr(
            "Читает чертёж DXF и раскладывает его по слоям GeoPackage: отдельный слой на "
            "каждый слой чертежа и вид геометрии, в выбранной системе координат.\n\n"
            "Атрибуты блоков становятся полями таблицы, подписи привязываются к полигонам, "
            "внутрь которых попали, замкнутые полилинии читаются полигонами.\n\n"
            "<b>DWG не читается.</b> Сохраните чертёж в AutoCAD как DXF, либо переведите "
            "бесплатной программой LibreDWG (<a href=\"{url}\">{url}</a>) командой "
            "«dwg2dxf -o чертёж.dxf чертёж.dwg» — модуль её не вызывает, перевод делается "
            "вручную. Встроенное окно QGIS «Проект → Импорт/Экспорт → Импорт слоёв из "
            "DWG/DXF» читает DWG само, но без атрибутов блоков."
        ).format(url=LIBREDWG_SITE)

    def initAlgorithm(self, config=None):
        add(self, QgsProcessingParameterFile(
            self.INPUT, self.tr("Чертёж DXF"),
            # «все файлы» нужны, чтобы выбранный DWG дошёл до нашего понятного
            # отказа, а не был отвергнут Processing как «неверное значение»
            fileFilter=self.tr("Чертежи DXF (*.dxf *.DXF);;Все файлы (*.*)")),
            self.tr("Только DXF. Чертёж DWG откройте в AutoCAD и сохраните как DXF."))
        add(self, QgsProcessingParameterCrs(
            self.CRS, self.tr("Система координат чертежа"), defaultValue="ProjectCrs"),
            self.tr("В самом DXF системы координат нет, поэтому её нужно знать и выбрать: "
                    "по умолчанию подставлена СК проекта. Если выбрать не ту, слои лягут "
                    "не на место."))
        add(self, QgsProcessingParameterEnum(
            self.LABELS, self.tr("Подписи чертежа"), options=LABEL_MODES,
            defaultValue=BIND_LABELS),
            self.tr("«Привязать к полигонам»: подпись попадает в поле «text» наименьшего "
                    "контура, внутрь которого легла; объект, собравший больше трёх подписей, "
                    "не получает ни одной — это чужой текст внутри контура. Непривязанные "
                    "подписи всё равно сохраняются отдельным слоем точек."))
        add(self, QgsProcessingParameterBoolean(
            self.SPLIT_BY_LAYER, self.tr("Отдельный слой на каждый слой чертежа"),
            defaultValue=True),
            self.tr("Иначе весь чертёж ляжет в три слоя — точки, линии и полигоны."))
        add(self, QgsProcessingParameterBoolean(
            self.CLOSED_AS_POLYGON, self.tr("Замкнутые полилинии — полигонами"),
            defaultValue=True),
            self.tr("Иначе контуры участков и зон останутся линиями, и площадь по ним "
                    "не посчитать."), advanced=True)
        add(self, QgsProcessingParameterBoolean(
            self.BLOCK_ATTRS, self.tr("Атрибуты блоков — в поля таблицы"), defaultValue=True),
            self.tr("Имена полей берутся из имён атрибутов блока (NOMER, ТИП). "
                    "Без этого значения атрибутов попадут в чертёж подписями."), advanced=True)
        add(self, QgsProcessingParameterEnum(
            self.BLOCKS, self.tr("Вставки блоков"), options=BLOCK_MODES, defaultValue=0),
            self.tr("«Одним объектом» — вставка блока становится одной записью таблицы, как "
                    "она и выглядит в AutoCAD. «Разобрать на фигуры» — каждая фигура внутри "
                    "блока отдельной записью; объектов станет в разы больше."), advanced=True)
        add(self, QgsProcessingParameterBoolean(
            self.KEEP_Z, self.tr("Сохранять высоты (Z)"), defaultValue=False),
            self.tr("Высоты из чертежа редко бывают осмысленными, поэтому по умолчанию "
                    "слои плоские."), advanced=True)
        add(self, QgsProcessingParameterEnum(
            self.ENCODING, self.tr("Кодировка текста"), options=ENCODINGS,
            defaultValue=AUTO_ENCODING),
            self.tr("Обычно определяется сама по содержимому файла. Задавать вручную стоит, "
                    "только если имена слоёв или подписи пришли кракозябрами."), advanced=True)
        add(self, QgsProcessingParameterFileDestination(
            self.OUTPUT, self.tr("GeoPackage со слоями чертежа"),
            fileFilter="GeoPackage (*.gpkg)"),
            self.tr("Один файл, внутри — по слою на каждый слой чертежа и вид геометрии. "
                    "Слои сразу добавляются в проект."))

    def processAlgorithm(self, parameters, context, feedback):
        try:
            from osgeo import gdal, ogr, osr  # входят в состав QGIS
        except ImportError:
            raise QgsProcessingException(self.tr(
                "Не найдены библиотеки GDAL для Python — без них DXF не прочитать. Обычно они входят в состав QGIS: переустановите QGIS"))
        gdal.UseExceptions()

        path = self.parameterAsFile(parameters, self.INPUT, context)
        if not path or not os.path.exists(path):
            raise QgsProcessingException(self.tr("Файл чертежа не найден — выберите существующий файл DXF"))
        self.check_kind(path)

        crs = self.parameterAsCrs(parameters, self.CRS, context)
        if not crs.isValid():
            raise QgsProcessingException(self.tr(
                "Выберите систему координат чертежа: в самом DXF её нет"))
        self.report_prj(path, crs, feedback)

        out_path = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        closed = self.parameterAsBool(parameters, self.CLOSED_AS_POLYGON, context)
        label_mode = self.parameterAsEnum(parameters, self.LABELS, context)
        with_attrs = self.parameterAsBool(parameters, self.BLOCK_ATTRS, context)
        explode = self.parameterAsEnum(parameters, self.BLOCKS, context) == 1
        split = self.parameterAsBool(parameters, self.SPLIT_BY_LAYER, context)
        keep_z = self.parameterAsBool(parameters, self.KEEP_Z, context)
        choice = self.parameterAsEnum(parameters, self.ENCODING, context)

        encoding = self.encoding_for(path, choice, feedback)
        # разбор нужен всегда: из него берутся дескрипторы подписей и атрибутов
        read = dxf_read.insert_attributes(path, encoding)
        attrib_handles, label_handles = read.attrib_handles, read.label_handles
        attrs = read.inserts if with_attrs else {}
        if read.inserts:
            with_values = sum(1 for i in read.inserts.values() if i.values)
            feedback.pushInfo(self.tr("Вставок блоков: {}, из них с атрибутами: {}").format(
                len(read.inserts), with_values))

        options = {
            "DXF_ENCODING": encoding,
            "DXF_MERGE_BLOCK_GEOMETRIES": "FALSE" if explode else None,
        }
        with gdal_options(gdal, options):
            ds = self.open_drawing(gdal, path)
            layer = ds.GetLayer(0)
            marks = (attrib_handles, label_handles)
            groups, labels, extent = self.scan(ogr, layer, marks, attrs, label_mode,
                                               split, keep_z, closed, feedback)
            self.check_coordinates(crs, extent, feedback)
            if not groups and not labels:
                raise QgsProcessingException(self.tr("В чертеже нет объектов с геометрией — только служебные записи. Проверьте, что чертёж сохранён вместе с пространством модели"))
            written = self.write(gdal, ogr, osr, layer, out_path, crs, groups, labels, attrs,
                                 marks, label_mode, split, keep_z, closed, feedback)
            ds = None

        self.check_temporary(out_path, feedback)
        for name in written:
            uri = "{}|layername={}".format(out_path, name)
            context.addLayerToLoadOnCompletion(
                uri, QgsProcessingContext.LayerDetails(name, context.project(), name))
        feedback.pushInfo(self.tr("Слоёв записано: {}").format(len(written)))
        return {self.OUTPUT: out_path, "LAYERS": written}

    # — подготовка —

    def check_kind(self, path):
        """Понятный отказ на DWG и двоичном DXF."""
        kind = dxf_read.file_kind(path)
        if kind == "dwg":
            raise QgsProcessingException(self.tr(
                "Это файл DWG, а не DXF — модуль читает только DXF. Три способа получить "
                "из него DXF:\n"
                "1. AutoCAD: «Сохранить как» → DXF. Ничего не теряется.\n"
                "2. Бесплатная программа LibreDWG ({url}): поставить её и перевести чертёж "
                "самому — командой «dwg2dxf -o чертёж.dxf чертёж.dwg», затем открыть "
                "полученный DXF этим окном. Модуль её не вызывает, DWG 2004–2018 она "
                "читает не полностью — сверьте число объектов.\n"
                "3. Встроенное окно QGIS «Проект → Импорт/Экспорт → Импорт слоёв из "
                "DWG/DXF» читает DWG само, но не делает из атрибутов блоков поля таблицы."
            ).format(url=LIBREDWG_SITE))
        if kind == "dxf-binary":
            raise QgsProcessingException(self.tr(
                "Это двоичный DXF, он не читается. Сохраните чертёж обычным (текстовым) DXF."))
        if kind != "dxf":
            raise QgsProcessingException(self.tr("Файл не похож на чертёж DXF. Нужен текстовый DXF; если это DWG, сохраните его в AutoCAD как DXF"))

    def encoding_for(self, path, choice, feedback):
        """Кодировка: выбранная или распознанная по содержимому файла."""
        if choice != AUTO_ENCODING:
            return ENCODINGS[choice]
        found = dxf_read.detect_encoding(path)
        declared = dxf_read.declared_codepage(path)
        feedback.pushInfo(self.tr("Кодировка текста в файле: {}").format(found))
        if declared and found.upper().replace("-", "") == "UTF8" and "1251" in declared:
            feedback.pushInfo(self.tr(
                "В заголовке чертежа объявлена {} — по ней имена слоёв читались бы "
                "кракозябрами; текст читается как UTF-8").format(declared))
        return found

    def report_prj(self, path, crs, feedback):
        """Сказать про .prj рядом с чертежом — он мог остаться от выгрузки."""
        prj = os.path.splitext(path)[0] + ".prj"
        if not os.path.exists(prj):
            return
        try:
            with open(prj, "r", encoding="utf-8", errors="replace") as f:
                wkt = f.read().strip()
        except OSError:
            return
        other = QgsCoordinateReferenceSystem.fromWkt(wkt)
        if other.isValid() and other != crs:
            feedback.pushWarning(self.tr(
                "Рядом с чертежом есть .prj с системой координат «{}» — она не совпадает "
                "с выбранной «{}»").format(other.description(), crs.description()))

    def check_temporary(self, out_path, feedback):
        """Сказать, если GeoPackage попал во временную папку: он пропадёт."""
        if is_temporary(out_path):
            feedback.pushWarning(self.tr(
                "GeoPackage записан во временную папку и пропадёт при очистке временных "
                "файлов, а слои в проекте перестанут открываться. Чтобы сохранить: "
                "правый щелчок по слою → «Экспорт» → «Сохранить объекты как…», или "
                "запустите снова, указав постоянный файл."))

    def check_coordinates(self, crs, extent, feedback):
        """Предупредить, если координаты чертежа не вяжутся с выбранной СК."""
        if crs.isGeographic() and extent.beyond_degrees():
            x, y = extent.largest()
            feedback.pushWarning(self.tr(
                "Координаты чертежа доходят до ({:.0f}, {:.0f}) — это метры, а выбрана СК "
                "в градусах «{}». Слои лягут не на место: выберите систему координат "
                "чертежа (МСК, UTM, Гаусса-Крюгера)."
            ).format(x, y, crs.description()))

    def open_drawing(self, gdal, path):
        try:
            ds = gdal.OpenEx(path, gdal.OF_VECTOR)
        except Exception as e:
            raise QgsProcessingException(self.tr("Чертёж не открылся: {}. Проверьте, что файл цел и открывается в AutoCAD").format(e))
        if ds is None or ds.GetLayerCount() == 0:
            raise QgsProcessingException(self.tr("В чертеже нет данных — похоже, файл пуст или испорчен. Откройте его в AutoCAD и сохраните заново"))
        return ds

    # — первый проход —

    def scan(self, ogr, layer, marks, attrs, label_mode, split, keep_z, closed, feedback):
        """Какие слои получатся, какие у них поля, где подписи и каков охват чертежа."""
        groups = {}
        labels = []
        extent = Extent()
        layer.ResetReading()
        total = max(layer.GetFeatureCount(), 1)
        for n, feature in enumerate(layer):
            if feedback.isCanceled():
                return groups, labels
            if n % 5000 == 0:
                feedback.setProgress(40.0 * n / total)
            sub = feature.GetFieldAsString("SubClasses")
            handle = feature.GetFieldAsString("EntityHandle")
            if is_attribute(sub, handle, marks[0]):
                continue  # значение атрибута: станет полем своего объекта
            geom = feature.GetGeometryRef()
            if geom is None or geom.IsEmpty():
                continue
            extent.add(geom.GetEnvelope())
            dxf_layer = feature.GetFieldAsString("Layer")
            if is_label(sub, handle, marks[1]):
                if label_mode == NO_LABELS:
                    continue
                point = geom.GetPoint(0)
                labels.append((QgsPointXY(point[0], point[1]),
                               feature.GetFieldAsString("Text"), dxf_layer, handle))
                continue
            info = attrs.get(handle)
            for kind, parts in split_by_kind(ogr, geom, closed).items():
                group = self.group_for(groups, dxf_layer, kind, split)
                group.count += 1
                if keep_z and any(p.Is3D() for p in parts):
                    group.has_z = True  # высоты есть хоть у одного объекта — слой объёмный
                if info is not None:
                    for tag, _value in info.values:
                        if tag not in group.tags:
                            group.tags.append(tag)
        return groups, labels, extent

    def group_for(self, groups, dxf_layer, kind, split, create=True):
        """Группа (будущий слой) для вида геометрии; при `create` создаёт новую.

        Высоты в ключ не входят: иначе слой чертежа, где часть объектов плоская,
        а часть с Z, разъехался бы на два слоя с номером в имени.
        """
        key = (dxf_layer if split else "", kind)
        group = groups.get(key)
        if group is None and create:
            group = groups[key] = Group(layer_name(dxf_layer if split else "Чертёж", kind), kind)
        return group

    # — привязка подписей —

    def bindings(self, ogr, layer, index, rows, marks, closed, feedback):
        """Какому объекту какие подписи достанутся.

        Подпись уходит в наименьший по площади полигон, который её содержит:
        иначе подписи внутренних участков забирала бы внешняя зона. Объект,
        собравший больше MAX_BOUND_LABELS подписей, не получает ни одной —
        это не его подпись, а чужой текст внутри контура.
        """
        best = {}  # номер подписи → (площадь, дескриптор объекта)
        layer.ResetReading()
        total = max(layer.GetFeatureCount(), 1)
        for n, feature in enumerate(layer):
            if feedback.isCanceled():
                return {}
            if n % 5000 == 0:
                feedback.setProgress(40.0 + 15.0 * n / total)
            sub = feature.GetFieldAsString("SubClasses")
            handle = feature.GetFieldAsString("EntityHandle")
            if is_label(sub, handle, marks[1]) or is_attribute(sub, handle, marks[0]):
                continue
            geom = feature.GetGeometryRef()
            if geom is None or geom.IsEmpty():
                continue
            parts = split_by_kind(ogr, geom, closed).get(POLYGON)
            if not parts:
                continue
            area = sum(part.GetArea() for part in parts)
            for i in self.labels_inside(ogr, parts, index, rows):
                if i not in best or area < best[i][0]:
                    best[i] = (area, handle)
        found = {}
        for i, (_area, handle) in best.items():
            found.setdefault(handle, []).append(i)
        return dict((handle, sorted(ids, key=lambda i: (-rows[i][0].y(), rows[i][0].x())))
                    for handle, ids in found.items() if len(ids) <= MAX_BOUND_LABELS)

    # — второй проход —

    def write(self, gdal, ogr, osr, layer, out_path, crs, groups, labels, attrs, marks,
              label_mode, split, keep_z, closed, feedback):
        """Объекты в GeoPackage, подписи — в полигоны или отдельным слоем."""
        index, label_rows = self.label_index(labels)
        if labels:
            feedback.pushInfo(self.tr("Подписей в чертеже: {}").format(len(labels)))
        bound = {}
        if label_mode == BIND_LABELS and index is not None:
            bound = self.bindings(ogr, layer, index, label_rows, marks, closed, feedback)
        if feedback.isCanceled():
            return []  # отменили на привязке — файл не создаём
        out_ds, srs = self.create_output(gdal, ogr, osr, out_path, crs, feedback)
        types = ogr_types(ogr)
        names = []
        used = set()
        for key in sorted(groups, key=lambda k: groups[k].name):
            group = groups[key]
            group.name = free_name(group.name, used)
            group.build_fields()
            flat, with_z = types[group.kind]
            group.layer = out_ds.CreateLayer(
                group.name, srs, with_z if group.has_z else flat,
                ["GEOMETRY_NAME=geom", "SPATIAL_INDEX=YES", "OVERWRITE=YES"])
            if group.layer is None:
                raise QgsProcessingException(
                    self.tr("Слой «{}» не создался в GeoPackage — возможно, файл открыт в другой программе. Закройте его и повторите").format(group.name))
            for name in group.fields:
                group.layer.CreateField(ogr.FieldDefn(name, ogr.OFTString))
            names.append(group.name)

        used_labels = set()
        out_ds.StartTransaction()
        layer.ResetReading()
        total = max(layer.GetFeatureCount(), 1)
        for n, feature in enumerate(layer):
            if feedback.isCanceled():
                break
            if n % 5000 == 0:
                feedback.setProgress(55.0 + 40.0 * n / total)
            sub = feature.GetFieldAsString("SubClasses")
            handle = feature.GetFieldAsString("EntityHandle")
            if is_label(sub, handle, marks[1]) or is_attribute(sub, handle, marks[0]):
                continue  # подписи и значения атрибутов — отдельно
            geom = feature.GetGeometryRef()
            if geom is None or geom.IsEmpty():
                continue
            dxf_layer = feature.GetFieldAsString("Layer")
            info = attrs.get(handle)
            entity = entity_name(sub)
            # у штриховки в поле Text GDAL держит имя узора, а не подпись
            own_text = "" if entity == "HATCH" else feature.GetFieldAsString("Text")
            for kind, parts in split_by_kind(ogr, geom, closed).items():
                group = self.group_for(groups, dxf_layer, kind, split, create=False)
                if group is None or group.layer is None:
                    continue
                out_geom = self.one_geometry(ogr, parts, group, types)
                text = own_text
                if kind == POLYGON and handle in bound:
                    used_labels.update(bound[handle])
                    if not text:
                        text = LABEL_JOIN.join(label_rows[i][1] for i in bound[handle])
                out = ogr.Feature(group.layer.GetLayerDefn())
                out.SetGeometry(out_geom)
                set_field(out, "dxf_layer", dxf_layer)
                set_field(out, "entity", entity or ("BLOCKREFERENCE" if info else ""))
                set_field(out, "handle", handle)
                set_field(out, "linetype", feature.GetFieldAsString("Linetype"))
                set_field(out, "text", text)
                if info is not None:
                    set_field(out, "block", info.name)
                    for tag, value in info.values:
                        set_field(out, group.field_of.get(tag), value)
                group.layer.CreateFeature(out)
        out_ds.CommitTransaction()

        if label_mode == BIND_LABELS:
            rest = [row for i, row in enumerate(label_rows) if i not in used_labels]
            bound = len(label_rows) - len(rest)
            if bound:
                feedback.pushInfo(self.tr("Подписей привязано к полигонам: {}").format(bound))
        else:
            rest = label_rows
        if rest:
            names += self.write_labels(ogr, out_ds, srs, rest, used, feedback)
        out_ds = None  # закрывает GeoPackage
        return names

    def create_output(self, gdal, ogr, osr, out_path, crs, feedback):
        """Создать GeoPackage или открыть существующий, не тронув чужие слои."""
        driver = ogr.GetDriverByName("GPKG")
        if driver is None:
            raise QgsProcessingException(self.tr("В этой сборке GDAL нет записи GeoPackage — переустановите QGIS"))
        out_ds = None
        if os.path.exists(out_path):
            try:
                out_ds = gdal.OpenEx(out_path, gdal.OF_VECTOR | gdal.OF_UPDATE)
            except Exception:
                out_ds = None
            if out_ds is None:
                raise QgsProcessingException(self.tr(
                    "Файл «{}» уже есть, и это не GeoPackage. Укажите другой файл."
                ).format(os.path.basename(out_path)))
            existing = out_ds.GetLayerCount()
            if existing:
                feedback.pushWarning(self.tr(
                    "Файл «{}» уже существует, слоёв в нём: {}. Слои чертежа будут "
                    "добавлены, одноимённые — заменены, остальные останутся как были."
                ).format(os.path.basename(out_path), existing))
        else:
            # CreateDataSource, а не Create: у драйвера OGR в GDAL 3.3.2 (QGIS 3.40)
            # метода Create нет
            out_ds = driver.CreateDataSource(out_path)
        if out_ds is None:
            raise QgsProcessingException(
                self.tr("Не удалось создать файл «{}». Проверьте, что папка существует и доступна для записи").format(out_path))
        return out_ds, self.spatial_reference(osr, crs)

    def spatial_reference(self, osr, crs):
        """СК для GDAL: по коду справочника, иначе описанием.

        По коду — потому что от описания из QGIS GDAL ругается на несовпадение с
        официальным и заводит в GeoPackage свою запись СК. Но код пользовательской
        СК («USER:100000», так выглядит МСК) GDAL не понимает: на 3.44 и 4 он
        бросает «OGR Error: Corrupt data», и алгоритм обрывался трассировкой.
        """
        srs = osr.SpatialReference()
        if crs.authid().upper().startswith(AUTHORITIES):
            try:
                if srs.SetFromUserInput(crs.authid()) == 0:
                    return srs
            except RuntimeError:
                pass
        srs = osr.SpatialReference()
        try:
            if srs.ImportFromWkt(crs.toWkt()) == 0:
                # у СК из строки PROJ названия нет («unknown»), а имя МСК живёт
                # только в реестре QGIS — переносим его в файл сами.
                # SetProjCS, а не SetName: в привязках Python SetName нет.
                if crs.description() and srs.GetName() in (None, "", "unknown"):
                    srs.SetProjCS(crs.description())
                return srs
        except RuntimeError:
            pass
        raise QgsProcessingException(self.tr(
            "Не удалось записать систему координат «{}» в GeoPackage. Выберите другую "
            "систему координат в поле «Система координат чертежа»."
        ).format(crs.description() or crs.authid() or "без названия"))

    def one_geometry(self, ogr, parts, group, types):
        """Фигуры одного вида → одна геометрия типа слоя."""
        flat, with_z = types[group.kind]
        target = with_z if group.has_z else flat
        out = ogr.Geometry(target)
        for part in parts:
            piece = part.Clone()
            if group.has_z:
                piece.Set3D(True)
            else:
                piece.FlattenTo2D()
            if ogr.GT_Flatten(piece.GetGeometryType()) == ogr.GT_Flatten(target):
                for i in range(piece.GetGeometryCount()):  # уже множественная
                    out.AddGeometry(piece.GetGeometryRef(i))
            else:
                out.AddGeometry(piece)
        return out

    # — подписи —

    def label_index(self, labels):
        """Пространственный индекс подписей: их мало, а объектов много."""
        if not labels:
            return None, []
        index = QgsSpatialIndex()
        rows = []
        for i, row in enumerate(labels):
            feature = QgsFeature(i)
            feature.setGeometry(QgsGeometry.fromPointXY(row[0]))
            index.addFeature(feature)
            rows.append(row)
        return index, rows

    def labels_inside(self, ogr, parts, index, rows):
        """Номера подписей, попавших внутрь этих контуров."""
        found = []
        for part in parts:
            x_min, x_max, y_min, y_max = part.GetEnvelope()
            for i in index.intersects(QgsRectangle(x_min, y_min, x_max, y_max)):
                if not rows[i][1] or i in found:
                    continue
                point = ogr.Geometry(ogr.wkbPoint)
                point.AddPoint_2D(rows[i][0].x(), rows[i][0].y())
                if part.Contains(point):
                    found.append(i)
        return found

    def write_labels(self, ogr, out_ds, srs, rows, used, feedback):
        """Подписи, не попавшие ни в один полигон, — отдельным слоем точек."""
        name = free_name(layer_name("Подписи", POINT), used)
        layer = out_ds.CreateLayer(name, srs, ogr.wkbPoint,
                                   ["GEOMETRY_NAME=geom", "SPATIAL_INDEX=YES", "OVERWRITE=YES"])
        if layer is None:
            raise QgsProcessingException(
                self.tr("Слой «{}» не создался в GeoPackage — возможно, файл открыт в другой программе. Закройте его и повторите").format(name))
        for field in ("dxf_layer", "handle", "text"):
            layer.CreateField(ogr.FieldDefn(field, ogr.OFTString))
        out_ds.StartTransaction()
        for point, text, dxf_layer, handle in rows:
            feature = ogr.Feature(layer.GetLayerDefn())
            geom = ogr.Geometry(ogr.wkbPoint)
            geom.AddPoint_2D(point.x(), point.y())
            feature.SetGeometry(geom)
            set_field(feature, "dxf_layer", dxf_layer)
            set_field(feature, "handle", handle)
            set_field(feature, "text", text)
            layer.CreateFeature(feature)
        out_ds.CommitTransaction()
        feedback.pushInfo(self.tr("Подписей отдельным слоем: {}").format(len(rows)))
        return [name]


def set_field(feature, name, value):
    """Записать значение, пустое — оставить NULL, а не пустой строкой."""
    if name and value not in (None, ""):
        feature.SetField(name, value)


class gdal_options(object):
    """Настройки GDAL на время чтения, с возвратом прежних значений."""

    def __init__(self, gdal, values):
        self.gdal = gdal
        self.values = values
        self.previous = {}

    def __enter__(self):
        for key, value in self.values.items():
            self.previous[key] = self.gdal.GetConfigOption(key)
            self.gdal.SetConfigOption(key, value)
        return self

    def __exit__(self, *exc):
        for key, value in self.previous.items():
            self.gdal.SetConfigOption(key, value)
        return False
