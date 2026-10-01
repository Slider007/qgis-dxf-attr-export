"""Проверки модуля без интерфейса QGIS. Запуск: tests/run_tests.sh

Настройки QGIS уводятся во временный профиль tests/_profile, файлы DXF
пишутся в tests/_out. Результат читается независимо от кода модуля: разбором
пар «код — значение» и драйвером DXF из GDAL (как его прочтёт другая программа).
"""

import os
import shutil
import sys
import unittest
import warnings

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from qgis.PyQt.QtCore import QCoreApplication, QSettings  # noqa: E402

PROFILE = os.path.join(HERE, "_profile")
OUT = os.path.join(HERE, "_out")
shutil.rmtree(PROFILE, ignore_errors=True)
shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(OUT)
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, PROFILE)
QCoreApplication.setOrganizationName("dxf-attr-export-tests")
QCoreApplication.setApplicationName("dxf-attr-export-tests")

from osgeo import ogr  # noqa: E402
from qgis.core import (  # noqa: E402
    Qgis,
    QgsApplication,
    QgsCategorizedSymbolRenderer,
    QgsDxfExport,
    QgsFeature,
    QgsCoordinateTransformContext,
    QgsFillSymbol,
    QgsGeometry,
    QgsGradientFillSymbolLayer,
    QgsLinePatternFillSymbolLayer,
    QgsMarkerSymbol,
    QgsPalLayerSettings,
    QgsProcessingFeedback,
    QgsProcessingParameterDxfLayers,
    QgsProject,
    QgsRendererCategory,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt import sip  # noqa: E402
from qgis.PyQt.QtCore import QDate, QEvent  # noqa: E402
from qgis.PyQt.QtGui import QColor  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow, QMenu, QToolBar  # noqa: E402

app = QgsApplication([], True, PROFILE)
if os.environ.get("QGIS_PREFIX_PATH"):
    app.setPrefixPath(os.environ["QGIS_PREFIX_PATH"], True)
app.initQgis()
# initQgis() переносит настройки в профиль default организации: возвращаем во временный
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, PROFILE)
assert QSettings().fileName().startswith(PROFILE), QSettings().fileName()

# папка со встроенными модулями QGIS (там лежит processing) — рядом с python QGIS
for _p in os.environ.get("PYTHONPATH", "").split(os.pathsep):
    if os.path.isdir(os.path.join(_p, "plugins")):
        sys.path.append(os.path.join(_p, "plugins"))
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
import processing  # noqa: E402

with warnings.catch_warnings():
    warnings.simplefilter("error", DeprecationWarning)
    import dxf_attr_export  # noqa: E402
    from dxf_attr_export import dxf_blocks  # noqa: E402
    from dxf_attr_export.plugin import DxfAttrExportPlugin  # noqa: E402

ALG = "dxfattrexport:exportdxf"
CRS = "EPSG:32637"
LONG = "длинное значение " * 20  # 340 символов


# ---------- чтение DXF без кода модуля

CODEPAGES = {"ANSI_1251": "cp1251", "ANSI_1252": "cp1252", "ANSI_1250": "cp1250"}


def dxf_encoding(data):
    """Как кодировку определяет читатель DXF: по $ACADVER и $DWGCODEPAGE."""
    lines = [ln.strip() for ln in data[:8192].decode("latin-1").split("\n")]
    version = lines[lines.index("$ACADVER") + 2]
    if version >= "AC1021":  # R2007 и новее всегда UTF-8
        return "utf-8"
    return CODEPAGES.get(lines[lines.index("$DWGCODEPAGE") + 2], "cp1251")


def read_pairs(path, encoding=None):
    with open(path, "rb") as f:
        data = f.read()
    lines = data.decode(encoding or dxf_encoding(data)).split("\n")
    lines = [ln.rstrip("\r") for ln in lines]
    if lines[-1] == "":
        lines.pop()
    return [(lines[i].strip(), lines[i + 1]) for i in range(0, len(lines) - 1, 2)]


def section(pairs, name):
    out, inside = [], False
    for i, (c, v) in enumerate(pairs):
        if c == "2" and i and pairs[i - 1] == ("0", "SECTION"):
            inside = v.strip() == name
            continue
        if inside and c == "0" and v.strip() == "ENDSEC":
            break
        if inside:
            out.append((c, v))
    return out


def entities(pairs):
    out = []
    for c, v in pairs:
        if c == "0":
            out.append({"type": v.strip(), "tags": []})
        elif out:
            out[-1]["tags"].append((c, v))
    for e in out:
        for c, v in e["tags"]:
            e.setdefault(c, v)
    return out


def inserts_with_attribs(path):
    """[(имя блока, слой, (x, y), {тег: значение}, флаги атрибутов)] вставок в пространстве модели."""
    result = []
    for e in entities(section(read_pairs(path), "ENTITIES")):
        if e["type"] == "INSERT":
            result.append([e["2"], e["8"], (float(e["10"]), float(e["20"])), {}, set()])
        elif e["type"] == "ATTRIB":
            result[-1][3][e["2"]] = e["1"]
            result[-1][4].add(int(e["70"]))
    return result


# ---------- исходные слои с известными ответами

def make_layer(geom, name, fields, rows):
    layer = QgsVectorLayer("{}?crs={}&{}".format(geom, CRS, fields), name, "memory")
    feats = []
    for wkt, values in rows:
        f = QgsFeature(layer.fields())
        if wkt:
            f.setGeometry(QgsGeometry.fromWkt(wkt))
        f.setAttributes(values)
        feats.append(f)
    ok, _ = layer.dataProvider().addFeatures(feats)
    assert ok and layer.featureCount() == len(rows), name
    layer.updateExtents()
    return layer


def make_layers():
    pts = make_layer(
        "Point", "Опоры",
        "field=name:string&field=kind:string&field=n:integer&field=h:double&field=Дата установки:date",
        [("POINT(400000 6000000)", ["Опора №1", "анкерная", 1, 12.5, QDate(2024, 5, 1)]),
         ("POINT(400100 6000050)", ["Опора Ω2", "промежуточная", 2, None, None]),
         (None, ["без геометрии", "анкерная", 3, 1.0, None])])
    pts.setRenderer(QgsCategorizedSymbolRenderer("kind", [
        QgsRendererCategory("анкерная", QgsMarkerSymbol.createSimple({"color": "255,0,0", "size": "4"}), "а"),
        QgsRendererCategory("промежуточная", QgsMarkerSymbol.createSimple(
            {"color": "0,0,255", "size": "3", "name": "square"}), "п")]))
    label = QgsPalLayerSettings()
    label.fieldName = "name"
    label.enabled = True
    pts.setLabeling(QgsVectorLayerSimpleLabeling(label))
    pts.setLabelsEnabled(True)

    lines = make_layer(
        "LineString", "ВЛ", "field=name:string&field=kind:string",
        [("LINESTRING(400000 6000000, 400100 6000050, 400300 6000000)", ["ВЛ 220", "Линии ВЛ"]),
         ("MULTILINESTRING((400000 6000100, 400100 6000100),(400200 6000100, 400200 6000200))",
          ["отпайка", "Отпайки"])])
    lines.renderer().symbol().setColor(QColor(0, 160, 0))
    lines.renderer().symbol().setWidth(0.8)

    polys = make_layer(
        "Polygon", "Участки", "field=name:string&field=note:string(1000)",
        [("POLYGON((400000 5999900, 400200 5999900, 400200 5999800, 400000 5999800, 400000 5999900),"
          "(400050 5999880, 400060 5999880, 400060 5999870, 400050 5999870, 400050 5999880))",
          ["Участок 1", LONG])])
    QgsProject.instance().addMapLayers([pts, lines, polys])
    return pts, lines, polys


def layers_param(*pairs):
    return [QgsProcessingParameterDxfLayers.layerAsVariantMap(QgsDxfExport.DxfLayer(lyr, idx))
            for lyr, idx in pairs]


class ExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = DxfAttrExportPlugin(None)
        cls.plugin.initProcessing()
        cls.pts, cls.lines, cls.polys = make_layers()
        cls.path = os.path.join(OUT, "all.dxf")
        # у линий слой AutoCAD берётся из поля kind (индекс 1)
        cls.result = processing.run(ALG, {
            "LAYERS": layers_param((cls.pts, -1), (cls.lines, 1), (cls.polys, -1)),
            "CRS": CRS, "OUTPUT": cls.path})
        cls.inserts = inserts_with_attribs(cls.path)

    @classmethod
    def tearDownClass(cls):
        QgsApplication.processingRegistry().removeProvider(cls.plugin.provider)
        QgsProject.instance().removeAllMapLayers()

    def by_name(self, value):
        found = [i for i in self.inserts if i[3].get("NAME") == value]
        self.assertEqual(len(found), 1, value)
        return found[0]

    def test_one_block_per_feature(self):
        self.assertEqual(self.result["OUTPUT"], self.path)
        # 2 опоры с геометрией + 2 линии + 1 полигон; объект без геометрии не выгружается
        self.assertEqual(len(self.inserts), 5)
        self.assertEqual(len({i[0] for i in self.inserts}), 5, "имена блоков должны различаться")

    def test_attribute_values(self):
        _, layer, xy, attrs, flags = self.by_name("Опора №1")
        self.assertEqual(layer, "Опоры")
        self.assertEqual(xy, (400000.0, 6000000.0))
        self.assertEqual(attrs, {"NAME": "Опора №1", "KIND": "анкерная", "N": "1", "H": "12.5",
                                 "ДАТА_УСТАНОВКИ": "2024-05-01"})
        self.assertEqual(flags, {1}, "атрибуты по умолчанию скрыты")
        # NULL — пустая строка; символ вне CP1251 — код \U+XXXX, который понимает AutoCAD
        _, _, _, attrs, _ = self.by_name("Опора \\U+03A92")
        self.assertEqual(attrs["H"], "")
        self.assertEqual(attrs["ДАТА_УСТАНОВКИ"], "")
        # длинное значение укорочено до 250 символов
        _, layer, _, attrs, _ = self.by_name("Участок 1")
        self.assertEqual(layer, "Участки")
        self.assertEqual(attrs["NOTE"], LONG[:250])

    def test_layer_from_field(self):
        self.assertEqual(self.by_name("ВЛ 220")[1], "Линии ВЛ")
        self.assertEqual(self.by_name("отпайка")[1], "Отпайки")
        # точка вставки линии — середина по длине: 400000,6000000 → 400100,6000050 → 400300,6000000
        x, y = self.by_name("ВЛ 220")[2]
        seg1 = (100 ** 2 + 50 ** 2) ** 0.5
        seg2 = (200 ** 2 + 50 ** 2) ** 0.5
        t = ((seg1 + seg2) / 2 - seg1) / seg2
        self.assertAlmostEqual(x, 400100 + 200 * t, places=6)
        self.assertAlmostEqual(y, 6000050 - 50 * t, places=6)

    def test_text_encoding_matches_header(self):
        """Кириллица записана той кодировкой, которую объявляет сам файл.

        QGIS 4 пишет UTF-8 при заголовке ANSI_1251 — модуль это исправляет.
        """
        with open(self.path, "rb") as f:
            data = f.read()
        codec = dxf_encoding(data)
        self.assertEqual(codec, "cp1251")
        self.assertIn("Опора №1".encode(codec), data)
        self.assertNotIn("Опора №1".encode("utf-8"), data)
        data.decode(codec)  # файл целиком читается этой кодировкой

    def test_no_temporary_names_left(self):
        with open(self.path, "rb") as f:
            data = f.read()
        self.assertNotIn(b"QFX_", data)
        self.assertNotIn(b"__dxf_attr_key", data)
        layers = [e["2"] for e in entities(section(read_pairs(self.path), "TABLES")) if e["type"] == "LAYER"]
        for name in ("Опоры", "Линии ВЛ", "Отпайки", "Участки"):
            self.assertEqual(layers.count(name), 1, name)

    def test_handles_unique(self):
        pairs = read_pairs(self.path)
        handles = [int(v, 16) for name in ("TABLES", "BLOCKS", "ENTITIES", "OBJECTS")
                   for c, v in section(pairs, name) if c in ("5", "105")]
        self.assertEqual(len(handles), len(set(handles)))
        header = section(pairs, "HEADER")
        seed = next(int(header[i + 1][1], 16) for i, (c, v) in enumerate(header) if v.strip() == "$HANDSEED")
        self.assertGreater(seed, max(handles))
        units = next(int(header[i + 1][1]) for i, (c, v) in enumerate(header) if v.strip() == "$INSUNITS")
        self.assertEqual(units, 6, "единицы чертежа — метры")

    def test_block_contents(self):
        """В блоке объекта — описания атрибутов и его фигуры, точка вставки = базовая точка."""
        blocks, current = {}, None
        for e in entities(section(read_pairs(self.path), "BLOCKS")):
            if e["type"] == "BLOCK":
                current = e["2"]
                blocks[current] = {"base": (float(e["10"]), float(e["20"])), "items": []}
            elif e["type"] != "ENDBLK":
                blocks[current]["items"].append(e["type"])
        name, _, xy, _, _ = self.by_name("Участок 1")
        self.assertEqual(blocks[name]["base"], xy)
        self.assertEqual(sorted(blocks[name]["items"]), ["ATTDEF", "ATTDEF", "HATCH", "LWPOLYLINE", "LWPOLYLINE"])
        name = self.by_name("Опора №1")[0]
        self.assertIn("INSERT", blocks[name]["items"], "значок точки — вложенный блок QGIS")

    def test_gdal_reads_geometry_and_labels(self):
        """Другая программа видит те же координаты и площади, что в QGIS."""
        ds = ogr.Open(self.path)
        self.assertIsNotNone(ds)
        geoms, texts = [], []
        for f in ds.GetLayer(0):
            g = f.GetGeometryRef().Clone()
            if "AcDbMText" in (f.GetField("SubClasses") or ""):
                texts.append((f.GetField("Layer"), f.GetField("Text")))
            elif "AcDbBlockReference" in (f.GetField("SubClasses") or ""):
                geoms.append((f.GetField("Layer"), g))
        by_layer = {}
        for layer, g in geoms:
            by_layer.setdefault(layer, []).append(g)
        line = by_layer["Линии ВЛ"][0]
        self.assertEqual([(line.GetX(i), line.GetY(i)) for i in range(line.GetPointCount())],
                         [(400000, 6000000), (400100, 6000050), (400300, 6000000)])
        parts = by_layer["Участки"][0]
        areas = [parts.GetGeometryRef(i).GetArea() for i in range(parts.GetGeometryCount())
                 if parts.GetGeometryRef(i).GetGeometryName() == "POLYGON"]
        self.assertEqual(areas, [19900.0], "заливка полигона с дыркой: 200×100 − 10×10")
        self.assertIn(("Опоры", "Опора №1"), texts)

    def test_prj(self):
        with open(os.path.join(OUT, "all.prj"), encoding="utf-8") as f:
            self.assertIn("UTM_Zone_37N", f.read())


class OptionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = DxfAttrExportPlugin(None)
        cls.plugin.initProcessing()
        cls.pts, cls.lines, cls.polys = make_layers()

    @classmethod
    def tearDownClass(cls):
        QgsApplication.processingRegistry().removeProvider(cls.plugin.provider)
        QgsProject.instance().removeAllMapLayers()

    def test_selected_only_and_visible(self):
        self.pts.selectByIds([f.id() for f in self.pts.getFeatures() if f["n"] == 2])
        path = os.path.join(OUT, "selected.dxf")
        processing.run(ALG, {"LAYERS": layers_param((self.pts, -1)), "CRS": CRS,
                             "SELECTED_FEATURES_ONLY": True, "ATTRIBUTES_VISIBLE": True,
                             "WRITE_PRJ": False, "OUTPUT": path})
        inserts = inserts_with_attribs(path)
        self.assertEqual([i[3]["N"] for i in inserts], ["2"])
        self.assertEqual(inserts[0][4], {0}, "атрибуты видимые")
        self.assertFalse(os.path.exists(os.path.join(OUT, "selected.prj")))

    def test_selected_only_without_selection(self):
        """Ничего не выделено — понятное сообщение, а не «нет объектов с геометрией»."""
        self.pts.removeSelection()
        self.polys.removeSelection()
        errors = []

        class Feedback(QgsProcessingFeedback):
            def reportError(self, error, fatalError=False):
                errors.append(error)

        with self.assertRaises(Exception):
            processing.run(ALG, {"LAYERS": layers_param((self.pts, -1), (self.polys, -1)), "CRS": CRS,
                                 "SELECTED_FEATURES_ONLY": True,
                                 "OUTPUT": os.path.join(OUT, "nosel.dxf")}, feedback=Feedback())
        self.assertTrue(any("ничего не выделено" in e for e in errors), errors)

    def test_selected_by_source_path(self):
        """Окно Processing передаёт слой путём к файлу: выделение должно учитываться."""
        path = os.path.join(OUT, "src.gpkg")
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.layerName = "Границы ИИ"
        QgsVectorFileWriter.writeAsVectorFormatV3(self.polys, path, QgsCoordinateTransformContext(), options)
        layer = QgsVectorLayer(path + "|layername=Границы ИИ", "Границы ИИ", "ogr")
        QgsProject.instance().addMapLayer(layer)
        layer.selectByIds([next(layer.getFeatures()).id()])
        out = os.path.join(OUT, "src.dxf")
        processing.run(ALG, {"LAYERS": [{"layer": layer.source(), "attributeIndex": -1}], "CRS": CRS,
                             "SELECTED_FEATURES_ONLY": True, "OUTPUT": out})
        self.assertEqual(len(inserts_with_attribs(out)), 1)

    def test_other_crs(self):
        """Координаты пересчитываются в выбранную СК, точка вставки тоже."""
        path = os.path.join(OUT, "wgs.dxf")
        processing.run(ALG, {"LAYERS": layers_param((self.pts, -1)), "CRS": "EPSG:4326", "OUTPUT": path})
        xy = [i[2] for i in inserts_with_attribs(path)]
        # 400000 E, 6000000 N зоны 37N; эталон посчитан pyproj отдельно от QGIS
        self.assertAlmostEqual(xy[0][0], 37.46929874, places=7)
        self.assertAlmostEqual(xy[0][1], 54.13837332, places=7)

    def test_existing_key_field_is_error(self):
        layer = make_layer("Point", "С ключом", "field=__dxf_attr_key:string",
                           [("POINT(400000 6000000)", ["x"])])
        QgsProject.instance().addMapLayer(layer)
        with self.assertRaises(Exception):
            processing.run(ALG, {"LAYERS": layers_param((layer, -1)), "CRS": CRS,
                                 "OUTPUT": os.path.join(OUT, "key.dxf")})


class OutlineTest(unittest.TestCase):
    """Режим «только границы»: в блоках полилинии и точки, без заливок и значков."""

    @classmethod
    def setUpClass(cls):
        cls.plugin = DxfAttrExportPlugin(None)
        cls.plugin.initProcessing()
        cls.pts, cls.lines, cls.polys = make_layers()
        # полигон без обводки: в полном оформлении у него только заливка
        cls.polys.renderer().setSymbol(QgsFillSymbol.createSimple(
            {"color": "200,200,0", "outline_style": "no"}))
        cls.path = os.path.join(OUT, "outline.dxf")
        processing.run(ALG, {
            "LAYERS": layers_param((cls.pts, -1), (cls.lines, 1), (cls.polys, -1)),
            "CRS": CRS, "BLOCK_CONTENT": 1, "OUTPUT": cls.path})
        cls.inserts = inserts_with_attribs(cls.path)
        cls.blocks, current = {}, None
        for e in entities(section(read_pairs(cls.path), "BLOCKS")):
            if e["type"] == "BLOCK":
                current = e["2"]
                cls.blocks[current] = []
            elif e["type"] not in ("ENDBLK", "ATTDEF"):
                cls.blocks[current].append(e)

    @classmethod
    def tearDownClass(cls):
        QgsApplication.processingRegistry().removeProvider(cls.plugin.provider)
        QgsProject.instance().removeAllMapLayers()

    def content(self, name):
        block = next(i[0] for i in self.inserts if i[3].get("NAME") == name)
        return self.blocks[block]

    def points_of(self, e):
        xs = [float(v) for c, v in e["tags"] if c == "10"]
        ys = [float(v) for c, v in e["tags"] if c == "20"]
        return list(zip(xs, ys))

    def test_polygon_rings(self):
        items = self.content("Участок 1")
        self.assertEqual([e["type"] for e in items], ["LWPOLYLINE", "LWPOLYLINE"], "без HATCH")
        self.assertEqual([int(e["70"]) for e in items], [1, 1], "контуры замкнуты")
        self.assertEqual(self.points_of(items[0]),
                         [(400000, 5999900), (400200, 5999900), (400200, 5999800), (400000, 5999800)])
        self.assertEqual(self.points_of(items[1]),
                         [(400050, 5999880), (400060, 5999880), (400060, 5999870), (400050, 5999870)])
        # обводки нет — цвет заливки 200,200,0
        self.assertEqual(int(items[0]["420"]), (200 << 16) | (200 << 8))

    def test_lines_and_points(self):
        items = self.content("ВЛ 220")
        self.assertEqual([e["type"] for e in items], ["LWPOLYLINE"])
        self.assertEqual(int(items[0]["70"]), 0)
        self.assertEqual(self.points_of(items[0]), [(400000, 6000000), (400100, 6000050), (400300, 6000000)])
        self.assertEqual(int(items[0]["420"]), 160 << 8)
        self.assertEqual(len(self.content("отпайка")), 2, "две части мультилинии")
        items = self.content("Опора №1")
        self.assertEqual([e["type"] for e in items], ["POINT"], "вместо значка — точка")
        self.assertEqual((float(items[0]["10"]), float(items[0]["20"])), (400000.0, 6000000.0))
        self.assertEqual(int(items[0]["420"]), 255 << 16, "цвет категории «анкерная»")

    def test_labels_and_attributes_kept(self):
        self.assertEqual(len(self.inserts), 5)
        attrs = next(i[3] for i in self.inserts if i[3]["NAME"] == "ВЛ 220")
        self.assertEqual(attrs["KIND"], "Линии ВЛ")
        texts = [e["1"] for e in entities(section(read_pairs(self.path), "ENTITIES")) if e["type"] == "MTEXT"]
        self.assertTrue(any("Опора" in t for t in texts))
        with open(self.path, "rb") as f:
            self.assertNotIn(b"QFX_", f.read())

    def test_full_mode_has_fill(self):
        """Для сравнения: в полном оформлении у того же полигона заливка HATCH."""
        path = os.path.join(OUT, "full.dxf")
        processing.run(ALG, {"LAYERS": layers_param((self.polys, -1)), "CRS": CRS, "OUTPUT": path})
        kinds = [e["type"] for e in entities(section(read_pairs(path), "BLOCKS"))]
        self.assertIn("HATCH", kinds)
        self.assertNotIn("LWPOLYLINE", kinds, "обводки у символа нет")


class PatternFillTest(unittest.TestCase):
    """Штриховку линиями QGIS в DXF не переносит — модуль рисует её сам."""

    ANGLE = 135.0   # градусы по часовой от севера, как в QGIS
    STEP_MM = 3.75  # шаг в миллиметрах; при масштабе 1:1000 это 3.75 м

    @classmethod
    def setUpClass(cls):
        cls.plugin = DxfAttrExportPlugin(None)
        cls.plugin.initProcessing()
        cls.polys = make_layer(
            "Polygon", "Зоны", "field=name:string",
            [("POLYGON((400000 6000000, 400100 6000000, 400100 6000100, 400000 6000100, 400000 6000000))",
              ["Зона 1"])])
        symbol = QgsFillSymbol()
        hatch = QgsLinePatternFillSymbolLayer()
        hatch.setLineAngle(cls.ANGLE)
        hatch.setDistance(cls.STEP_MM)
        hatch.setDistanceUnit(Qgis.RenderUnit.Millimeters)
        hatch.setColor(QColor(183, 72, 75))
        symbol.changeSymbolLayer(0, hatch)
        cls.polys.renderer().setSymbol(symbol)
        QgsProject.instance().addMapLayer(cls.polys)
        cls.path = os.path.join(OUT, "hatch.dxf")
        processing.run(ALG, {"LAYERS": layers_param((cls.polys, -1)), "CRS": CRS,
                            "SYMBOLOGY_SCALE": 1000, "OUTPUT": cls.path})

    @classmethod
    def tearDownClass(cls):
        QgsApplication.processingRegistry().removeProvider(cls.plugin.provider)
        QgsProject.instance().removeAllMapLayers()

    def hatch_lines(self, path):
        out = []
        for e in entities(section(read_pairs(path), "BLOCKS")):
            if e["type"] != "LWPOLYLINE" or int(e["70"]) != 0:
                continue
            xs = [float(v) for c, v in e["tags"] if c == "10"]
            ys = [float(v) for c, v in e["tags"] if c == "20"]
            if len(xs) == 2:
                out.append((list(zip(xs, ys)), int(e["420"])))
        return out

    def test_lines_drawn(self):
        lines = self.hatch_lines(self.path)
        # квадрат 100×100 м, шаг 3.75 м по нормали: диагональных линий около 37
        self.assertGreater(len(lines), 30)
        self.assertLess(len(lines), 45)
        self.assertEqual({rgb for _, rgb in lines}, {(183 << 16) | (72 << 8) | 75}, "цвет штриховки")

    def test_angle_and_step(self):
        import math
        lines = self.hatch_lines(self.path)
        for pts, _ in lines:
            (x0, y0), (x1, y1) = pts
            angle = math.degrees(math.atan2(y1 - y0, x1 - x0)) % 180
            self.assertAlmostEqual(angle, (90 - self.ANGLE) % 180, places=6, msg="угол линий")
        # расстояние между соседними линиями по нормали
        a = math.radians(90 - self.ANGLE)
        nx, ny = -math.sin(a), math.cos(a)
        offsets = sorted(pts[0][0] * nx + pts[0][1] * ny for pts, _ in lines)
        steps = [round(b - a2, 6) for a2, b in zip(offsets, offsets[1:])]
        self.assertEqual(set(steps), {3.75}, "шаг 3.75 мм при 1:1000 = 3.75 м")

    def test_lines_inside_polygon(self):
        lines = self.hatch_lines(self.path)
        for pts, _ in lines:
            for x, y in pts:
                self.assertTrue(399999.9 <= x <= 400100.1 and 5999999.9 <= y <= 6000100.1, (x, y))

    def test_can_be_switched_off(self):
        path = os.path.join(OUT, "hatch_off.dxf")
        processing.run(ALG, {"LAYERS": layers_param((self.polys, -1)), "CRS": CRS,
                            "SYMBOLOGY_SCALE": 1000, "PATTERN_FILLS": 2, "OUTPUT": path})
        self.assertEqual(self.hatch_lines(path), [])

    def test_native_hatch(self):
        """Штриховка AutoCAD: один объект на объект карты, узор задан в файле."""
        path = os.path.join(OUT, "hatch_native.dxf")
        processing.run(ALG, {"LAYERS": layers_param((self.polys, -1)), "CRS": CRS,
                            "SYMBOLOGY_SCALE": 1000, "PATTERN_FILLS": 1, "OUTPUT": path})
        hatches = [e for e in entities(section(read_pairs(path), "BLOCKS")) if e["type"] == "HATCH"]
        self.assertEqual(len(hatches), 1)
        h = hatches[0]
        self.assertEqual(h["2"], "_USER", "узор описан в файле, а не взят из acad.pat")
        self.assertEqual(int(h["70"]), 0, "не сплошная заливка")
        self.assertEqual(int(h["76"]), 0, "узор задан пользователем")
        self.assertAlmostEqual(float(h["53"]), (90 - self.ANGLE) % 180, places=6)
        self.assertAlmostEqual(float(h["46"]), 3.75, places=6)
        self.assertEqual(int(h["420"]), (183 << 16) | (72 << 8) | 75)
        # контур объекта: 4 точки квадрата
        self.assertEqual(int(h["91"]), 1)
        self.assertEqual(int(h["93"]), 4)
        # вместо десятков линий — один объект (на больших контурах это решает размер файла)
        self.assertEqual(self.hatch_lines(path), [])
        self.assertGreater(len(self.hatch_lines(self.path)), 30)

    def test_native_hatch_keeps_holes(self):
        layer = make_layer(
            "Polygon", "С дыркой", "field=name:string",
            [("POLYGON((400000 6000000, 400100 6000000, 400100 6000100, 400000 6000100, 400000 6000000),"
              "(400020 6000020, 400040 6000020, 400040 6000040, 400020 6000040, 400020 6000020))",
              ["дырка"])])
        layer.renderer().setSymbol(self.polys.renderer().symbol().clone())
        QgsProject.instance().addMapLayer(layer)
        path = os.path.join(OUT, "hatch_hole.dxf")
        processing.run(ALG, {"LAYERS": layers_param((layer, -1)), "CRS": CRS,
                            "SYMBOLOGY_SCALE": 1000, "PATTERN_FILLS": 1, "OUTPUT": path})
        h = next(e for e in entities(section(read_pairs(path), "BLOCKS")) if e["type"] == "HATCH")
        self.assertEqual(int(h["91"]), 2, "внешний контур и дырка")
        flags = [int(v) for c, v in h["tags"] if c == "92"]
        self.assertEqual(flags, [3, 2], "первый контур внешний, второй — дырка")

    def test_unsupported_fill_warns(self):
        layer = make_layer("Polygon", "Градиент", "field=name:string",
                           [("POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))", ["g"])])
        symbol = QgsFillSymbol()
        symbol.changeSymbolLayer(0, QgsGradientFillSymbolLayer(QColor(255, 0, 0), QColor(0, 0, 255)))
        layer.renderer().setSymbol(symbol)
        QgsProject.instance().addMapLayer(layer)
        errors = []

        class Feedback(QgsProcessingFeedback):
            def reportError(self, error, fatalError=False):
                errors.append(error)

        processing.run(ALG, {"LAYERS": layers_param((layer, -1)), "CRS": CRS,
                            "OUTPUT": os.path.join(OUT, "gradient.dxf")}, feedback=Feedback())
        self.assertTrue(any("градиент" in e for e in errors), errors)


class HelpersTest(unittest.TestCase):
    def test_tags(self):
        self.assertEqual(dxf_blocks.attribute_tags(["name", "Name", "Дата установки", "a:b", ""]),
                         ["NAME", "NAME_2", "ДАТА_УСТАНОВКИ", "A_B", "FIELD"])

    def test_names(self):
        self.assertEqual(dxf_blocks.safe_name('ВЛ 220/кВ: "А"'), "ВЛ 220_кВ_ _А_")
        self.assertEqual(dxf_blocks.safe_name("  "), "0")

    def test_text_value(self):
        self.assertEqual(dxf_blocks.text_value("a\nb\tc"), "a b c")
        self.assertEqual(dxf_blocks.text_value(None), "")
        self.assertEqual(dxf_blocks.text_value("x^y"), "x^ y")


class Iface:
    def __init__(self):
        self.window = QMainWindow()
        self.canvas = QgsMapCanvas(self.window)
        self.menu = []
        self.plugin_menu = QMenu("Модули")

    def mainWindow(self): return self.window
    def mapCanvas(self): return self.canvas
    def messageBar(self): return None
    def pluginMenu(self): return self.plugin_menu

    def addToolBar(self, name):
        bar = QToolBar(name)
        self.window.addToolBar(bar)
        sip.transferback(bar)
        return bar

    def addPluginToMenu(self, m, a):
        sub = next((x.menu() for x in self.plugin_menu.actions() if x.menu() is not None), None)
        sub = sub or self.plugin_menu.addMenu(m.replace("&", ""))
        sub.addAction(a)
        self.menu.append((m, a.text()))

    def removePluginMenu(self, m, a):
        self.menu.remove((m, a.text()))


# ---------- чертежи для импорта: пишутся руками, как их пишет чужая программа

IMPORT_ALG = "dxfattrexport:importdxf"


def dxf_text(entities, blocks=(), codepage="ANSI_1251"):
    """Минимальный DXF из пар «код — значение»."""
    def sec(name, body):
        return ["0", "SECTION", "2", name] + list(body) + ["0", "ENDSEC"]
    out = (sec("HEADER", ["9", "$ACADVER", "1", "AC1015", "9", "$DWGCODEPAGE", "3", codepage])
           + sec("BLOCKS", blocks) + sec("ENTITIES", entities) + ["0", "EOF"])
    return "\r\n".join(out) + "\r\n"


def write_dxf(name, entities, blocks=(), codepage="ANSI_1251", encoding="cp1251"):
    path = os.path.join(OUT, name)
    with open(path, "wb") as f:
        f.write(dxf_text(entities, blocks, codepage).encode(encoding))
    return path


def tag_poly(handle, layer, pts, closed=True):
    tags = ["0", "LWPOLYLINE", "5", handle, "8", layer, "100", "AcDbEntity",
            "100", "AcDbPolyline", "90", str(len(pts)), "70", "1" if closed else "0"]
    for x, y in pts:
        tags += ["10", repr(float(x)), "20", repr(float(y))]
    return tags


def tag_text(handle, layer, x, y, value):
    return ["0", "TEXT", "5", handle, "8", layer,
            "10", repr(float(x)), "20", repr(float(y)), "40", "2.5", "1", value]


def tag_insert(handle, layer, x, y, block, attribs=()):
    tags = ["0", "INSERT", "5", handle, "8", layer, "66", "1" if attribs else "0",
            "2", block, "10", repr(float(x)), "20", repr(float(y))]
    for i, (tag, value) in enumerate(attribs):
        tags += ["0", "ATTRIB", "5", "{}A{}".format(handle, i), "8", layer,
                 "10", repr(float(x)), "20", repr(float(y)), "40", "2.5",
                 "1", value, "2", tag, "70", "1"]
    if attribs:
        tags += ["0", "SEQEND", "5", "{}S".format(handle), "8", layer]
    return tags


def tag_block(name, handle, shapes):
    return (["0", "BLOCK", "5", handle, "8", "0", "2", name, "70", "0",
             "10", "0.0", "20", "0.0", "3", name, "1", ""] + list(shapes)
            + ["0", "ENDBLK", "5", handle + "E", "8", "0"])


SQUARE = [(0, 0), (100, 0), (100, 100), (0, 100)]


def read_gpkg(path):
    """GeoPackage → {имя слоя: [{поле: значение, "wkt": …, "type": …}]}."""
    ds = ogr.Open(path)
    assert ds is not None, path
    out = {}
    for i in range(ds.GetLayerCount()):
        layer = ds.GetLayer(i)
        rows = []
        for feature in layer:
            row = dict(feature.items())
            geom = feature.GetGeometryRef()
            row["wkt"] = geom.ExportToWkt() if geom else None
            rows.append(row)
        out[layer.GetName()] = {
            "rows": rows,
            "type": ogr.GeometryTypeToName(layer.GetGeomType()),
            "crs": (layer.GetSpatialRef().GetAuthorityCode(None)
                    if layer.GetSpatialRef() else None),
            "fields": [layer.GetLayerDefn().GetFieldDefn(n).GetName()
                       for n in range(layer.GetLayerDefn().GetFieldCount())],
        }
        layer.ResetReading()
    ds = None
    return out


class ImportTest(unittest.TestCase):
    """Чтение чертежа: слои, атрибуты блоков, подписи, кодировка."""

    @classmethod
    def setUpClass(cls):
        cls.plugin = DxfAttrExportPlugin(None)
        cls.plugin.initProcessing()
        cls.path = write_dxf(
            "import_src.dxf",
            tag_poly("100", "Границы", SQUARE)
            + tag_poly("101", "Дороги", [(0, 200), (100, 220)], closed=False)
            + tag_text("102", "Подписи", 50, 50, "ЗОУИТ-1")
            + tag_insert("110", "Опоры", 300, 50, "ОПОРА", [("NOMER", "17"), ("ТИП", "анкерная")]),
            tag_block("ОПОРА", "120", tag_poly("121", "Опоры", [(-2, -2), (2, -2), (2, 2), (-2, 2)])))

    @classmethod
    def tearDownClass(cls):
        QgsApplication.processingRegistry().removeProvider(cls.plugin.provider)

    def run_import(self, name, path=None, **params):
        out = os.path.join(OUT, name)
        values = {"INPUT": path or self.path, "CRS": CRS, "OUTPUT": out}
        values.update(params)
        processing.run(IMPORT_ALG, values)
        return read_gpkg(out)

    def test_layer_per_dxf_layer_and_kind(self):
        """Отдельный слой на каждый слой чертежа и вид геометрии, с выбранной СК."""
        got = self.run_import("imp_basic.gpkg")
        self.assertEqual(sorted(got), ["Границы (полигоны)", "Дороги (линии)", "Опоры (полигоны)"])
        self.assertEqual(got["Границы (полигоны)"]["type"], "Multi Polygon")
        self.assertEqual(got["Дороги (линии)"]["type"], "Multi Line String")
        self.assertEqual(got["Границы (полигоны)"]["crs"], CRS.split(":")[1])
        self.assertEqual(got["Границы (полигоны)"]["rows"][0]["wkt"],
                         "MULTIPOLYGON (((0 0,100 0,100 100,0 100,0 0)))")
        self.assertEqual(got["Дороги (линии)"]["rows"][0]["wkt"],
                         "MULTILINESTRING ((0 200,100 220))")

    def test_block_attributes_become_fields(self):
        """Атрибуты блока — поля таблицы, а не отдельные подписи."""
        got = self.run_import("imp_attrs.gpkg")["Опоры (полигоны)"]
        row = got["rows"][0]
        self.assertEqual(row["block"], "ОПОРА")
        self.assertEqual(row["nomer"], "17")
        self.assertEqual(row["тип"], "анкерная")
        # значения атрибутов не должны остаться ещё и подписями
        self.assertNotIn("Подписи (точки)", self.run_import("imp_attrs2.gpkg"))

    def test_attributes_can_be_switched_off(self):
        got = self.run_import("imp_noattrs.gpkg", BLOCK_ATTRS=False)["Опоры (полигоны)"]
        self.assertNotIn("nomer", got["fields"])
        self.assertIsNone(got["rows"][0]["block"])

    def test_label_goes_into_polygon(self):
        """Подпись внутри полигона попадает в его поле text."""
        got = self.run_import("imp_label.gpkg")
        self.assertEqual(got["Границы (полигоны)"]["rows"][0]["text"], "ЗОУИТ-1")
        self.assertNotIn("Подписи (точки)", got)

    def test_many_labels_stay_apart(self):
        """Много подписей в одном контуре — это чужой текст: привязки нет."""
        path = write_dxf("imp_many.dxf", tag_poly("100", "Границы", SQUARE)
                         + [t for i in range(4)
                            for t in tag_text("20{}".format(i), "Подписи", 10 + i * 10, 50,
                                              "точка {}".format(i))])
        got = self.run_import("imp_many.gpkg", path=path)
        self.assertEqual(got["Границы (полигоны)"]["rows"][0]["text"], None)
        self.assertEqual(len(got["Подписи (точки)"]["rows"]), 4)

    def test_labels_as_separate_layer(self):
        got = self.run_import("imp_labels_apart.gpkg", LABELS=1)
        self.assertEqual(got["Границы (полигоны)"]["rows"][0]["text"], None)
        self.assertEqual([r["text"] for r in got["Подписи (точки)"]["rows"]], ["ЗОУИТ-1"])

    def test_labels_can_be_skipped(self):
        got = self.run_import("imp_nolabels.gpkg", LABELS=2)
        self.assertNotIn("Подписи (точки)", got)
        self.assertEqual(got["Границы (полигоны)"]["rows"][0]["text"], None)

    def test_closed_polyline_as_line(self):
        """Без галочки замкнутая полилиния остаётся линией."""
        got = self.run_import("imp_lines.gpkg", CLOSED_AS_POLYGON=False)
        self.assertIn("Границы (линии)", got)
        self.assertNotIn("Границы (полигоны)", got)

    def test_single_layer_when_not_split(self):
        got = self.run_import("imp_one.gpkg", SPLIT_BY_LAYER=False)
        self.assertEqual(sorted(got), ["Чертёж (линии)", "Чертёж (полигоны)"])
        rows = got["Чертёж (полигоны)"]["rows"]
        self.assertEqual(sorted(r["dxf_layer"] for r in rows), ["Границы", "Опоры"])

    def test_layer_name_with_special_first_char(self):
        """Имя таблицы GeoPackage начинается с буквы: «!РКЗ» → «_!РКЗ»."""
        path = write_dxf("imp_bang.dxf", tag_poly("100", "!РКЗ", SQUARE))
        got = self.run_import("imp_bang.gpkg", path=path)
        self.assertEqual(sorted(got), ["_!РКЗ (полигоны)"])
        self.assertEqual(got["_!РКЗ (полигоны)"]["rows"][0]["dxf_layer"], "!РКЗ")

    def test_utf8_file_with_cp1251_header(self):
        """Конвертеры DWG→DXF пишут UTF-8, объявляя CP1251: имена не должны стать кракозябрами."""
        path = write_dxf("imp_utf8.dxf",
                         tag_poly("100", "Границы зон", SQUARE)
                         + tag_text("102", "Подписи", 50, 50, "ЗОУИТ-Ё"),
                         codepage="ANSI_1251", encoding="utf-8")
        got = self.run_import("imp_utf8.gpkg", path=path)
        self.assertIn("Границы зон (полигоны)", got)
        self.assertEqual(got["Границы зон (полигоны)"]["rows"][0]["text"], "ЗОУИТ-Ё")

    def test_dwg_is_refused_with_explanation(self):
        from qgis.core import QgsProcessingException
        path = os.path.join(OUT, "imp_fake.dwg")
        with open(path, "wb") as f:
            f.write(b"AC1032\x00\x00" + b"\x00" * 200)
        with self.assertRaises(QgsProcessingException) as caught:
            self.run_import("imp_dwg.gpkg", path=path)
        self.assertIn("DWG", str(caught.exception))
        self.assertIn("DXF", str(caught.exception))

    def test_round_trip_keeps_attributes(self):
        """Выгрузка и чтение обратно: значения полей возвращаются полями."""
        layer = make_layer("Point", "Опоры", "field=name:string&field=kind:string",
                           [("POINT(400000 6000000)", ["Опора №1", "анкерная"])])
        QgsProject.instance().addMapLayer(layer)
        try:
            dxf = os.path.join(OUT, "round.dxf")
            processing.run(ALG, {"LAYERS": layers_param((layer, -1)), "CRS": CRS, "OUTPUT": dxf})
            got = self.run_import("round.gpkg", path=dxf)
        finally:
            QgsProject.instance().removeMapLayer(layer.id())
        # значок точки выгружается фигурами, поэтому вид геометрии не важен
        names = [n for n in got if n.startswith("Опоры")]
        self.assertEqual(len(names), 1, sorted(got))
        rows = got[names[0]]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Опора №1")
        self.assertEqual(rows[0]["kind"], "анкерная")
        self.assertEqual(rows[0]["block"], "Опоры_1")

    def test_attribute_value_in_chunks(self):
        """Длинное значение атрибута пишется кусками (код 3) — склеивается обратно."""
        from dxf_attr_export import dxf_read
        tags = ["0", "SECTION", "2", "ENTITIES",
                "0", "INSERT", "5", "F0", "8", "Слой", "66", "1", "2", "БЛОК",
                "10", "0.0", "20", "0.0",
                "0", "ATTRIB", "5", "F1", "8", "Слой", "10", "0.0", "20", "0.0",
                "3", "начало ", "3", "середина ", "1", "конец", "2", "NOTE", "70", "0",
                "0", "SEQEND", "5", "F2", "8", "Слой", "0", "ENDSEC", "0", "EOF"]
        path = os.path.join(OUT, "chunks.dxf")
        with open(path, "wb") as f:
            f.write(("\r\n".join(["0", "SECTION", "2", "HEADER", "9", "$ACADVER",
                                   "1", "AC1015", "0", "ENDSEC"] + tags) + "\r\n").encode("cp1251"))
        read = dxf_read.insert_attributes(path)
        self.assertEqual(read.inserts["F0"].values, [("NOTE", "начало середина конец")])
        self.assertEqual(read.attrib_handles, {"F1"})


class PluginTest(unittest.TestCase):
    def test_load_unload(self):
        iface = Iface()
        plugin = dxf_attr_export.classFactory(iface)
        registry = QgsApplication.processingRegistry()
        plugin.initGui()
        self.assertIsNotNone(registry.algorithmById(ALG))
        bars = [b for b in iface.window.findChildren(QToolBar) if b.objectName() == "AltanEcoToolbar"]
        self.assertEqual(len(bars), 1)
        self.assertIsNotNone(registry.algorithmById(IMPORT_ALG))
        self.assertEqual([a.text() for a in bars[0].actions()],
                         ["Экспорт в DXF с атрибутами…", "Импорт DXF в ГИС-формат…"])
        self.assertEqual(iface.menu, [("&Альтан-Эко", "Экспорт в DXF с атрибутами…"),
                                      ("&Альтан-Эко", "Импорт DXF в ГИС-формат…")])
        plugin.unload()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertIsNone(registry.providerById("dxfattrexport"))
        self.assertEqual(iface.menu, [])
        self.assertEqual([b for b in iface.window.findChildren(QToolBar)
                          if b.objectName() == "AltanEcoToolbar"], [])

    def test_visible_layers(self):
        from dxf_attr_export.plugin import visible_vector_layers
        pts, lines, polys = make_layers()
        QgsProject.instance().layerTreeRoot().findLayer(lines.id()).setItemVisibilityChecked(False)
        self.assertEqual(visible_vector_layers(QgsProject.instance()), [pts, polys])
        QgsProject.instance().removeAllMapLayers()


if __name__ == "__main__":
    unittest.main(verbosity=2)
