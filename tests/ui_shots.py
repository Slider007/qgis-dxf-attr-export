"""Окна модуля для scripts/ui_snap.py из навыка qgis-ui-review.

Оба окна строит Processing по описанию параметров алгоритма — снимаем именно их,
потому что ничего другого пользователь не видит.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
sys.path.insert(0, PLUGIN_ROOT)

for _p in os.environ.get("PYTHONPATH", "").split(os.pathsep):
    if os.path.isdir(os.path.join(_p, "plugins")):
        sys.path.append(os.path.join(_p, "plugins"))

from qgis.gui import QgsBrowserGuiModel, QgsMapCanvas, QgsMessageBar  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402

from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsGeometry,
    QgsProject,
    QgsVectorLayer,
)

CRS = "EPSG:32637"


def _layers():
    """Пара слоёв в проекте: без них список слоёв в окне выгрузки пуст."""
    project = QgsProject.instance()
    if project.mapLayers():
        return
    project.setCrs(QgsCoordinateReferenceSystem(CRS))
    for name, geom, wkt in (("Опоры ВЛ 220 кВ", "Point", "POINT(400000 6000000)"),
                            ("Участки ЗОУИТ", "Polygon",
                             "POLYGON((400000 5999900,400200 5999900,400200 5999800,400000 5999800,"
                             "400000 5999900))")):
        layer = QgsVectorLayer("{}?crs={}&field=name:string&field=kind:string".format(geom, CRS),
                               name, "memory")
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromWkt(wkt))
        feature.setAttributes(["Опора №1", "анкерная"])
        layer.dataProvider().addFeatures([feature])
        layer.updateExtents()
        project.addMapLayer(layer)


def _provider():
    from dxf_attr_export.processing.provider import DxfAttrExportProvider
    registry = QgsApplication.processingRegistry()
    if registry.providerById("dxfattrexport") is None:
        provider = DxfAttrExportProvider()
        registry.addProvider(provider)
        _provider.keep = provider  # ссылку надо держать, иначе провайдер исчезнет


class Iface:
    """Минимальный iface: окно Processing без него не строится."""

    def __init__(self):
        self.window = QMainWindow()
        self.canvas = QgsMapCanvas(self.window)
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem(CRS))
        self.bar = QgsMessageBar(self.window)
        self.browser = QgsBrowserGuiModel()

    def mainWindow(self):
        return self.window

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return self.bar

    def activeLayer(self):
        return None

    def browserModel(self):
        return self.browser

    def messageTimeout(self):
        return 5


def _iface():
    import qgis.utils
    if getattr(qgis.utils, "iface", None) is None:
        qgis.utils.iface = _iface.keep = Iface()
    return qgis.utils.iface


def _dialog(alg_id):
    iface = _iface()
    from processing.core.Processing import Processing
    import processing.tools.general as general
    general.iface = iface  # модуль забрал iface при импорте, когда он был пуст
    Processing.initialize()
    _provider()
    _layers()
    import processing
    dialog = processing.createAlgorithmDialog(alg_id, {})
    # размер задаём после show(): иначе окно схлопнется до подсказанного размера,
    # а в QGIS оно открывается примерно таким
    dialog.show()
    dialog.resize(680, 520)
    return dialog


def windows():
    return [("экспорт", lambda: _dialog("dxfattrexport:exportdxf")),
            ("импорт", lambda: _dialog("dxfattrexport:importdxf"))]
