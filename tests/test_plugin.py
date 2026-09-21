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


class PluginTest(unittest.TestCase):
    def test_load_unload(self):
        iface = Iface()
        plugin = dxf_attr_export.classFactory(iface)
        registry = QgsApplication.processingRegistry()
        plugin.initGui()
        self.assertIsNotNone(registry.algorithmById(ALG))
        bars = [b for b in iface.window.findChildren(QToolBar) if b.objectName() == "AltanEcoToolbar"]
        self.assertEqual(len(bars), 1)
        self.assertEqual([a.text() for a in bars[0].actions()], ["Экспорт в DXF с атрибутами…"])
        self.assertEqual(iface.menu, [("&Альтан-Эко", "Экспорт в DXF с атрибутами…")])
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
