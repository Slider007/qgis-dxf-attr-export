import os

from qgis.core import Qgis, QgsApplication, QgsDxfExport, QgsProcessingParameterDxfLayers, QgsProject, QgsVectorLayer
from qgis.PyQt.QtGui import QIcon

from . import altan_toolbar
from .processing.provider import DxfAttrExportProvider

try:  # Qt6 / QGIS 4: QAction живёт в QtGui
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 / QGIS 3
    from qgis.PyQt.QtWidgets import QAction

PLUGIN_DIR = os.path.dirname(__file__)
ALGORITHM_ID = "dxfattrexport:exportdxf"


def visible_vector_layers(project):
    """Включённые в дереве слоёв векторные слои с геометрией, в порядке дерева."""
    return [lyr for lyr in project.layerTreeRoot().checkedLayers()
            if isinstance(lyr, QgsVectorLayer) and lyr.isSpatial()]


class DxfAttrExportPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.provider = None

    def initProcessing(self):
        self.provider = DxfAttrExportProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        self.initProcessing()
        self.action = QAction(
            QIcon(os.path.join(PLUGIN_DIR, "icon.svg")),
            "Экспорт в DXF с атрибутами…",
            self.iface.mainWindow(),
        )
        self.action.setToolTip(
            "Выгрузить слои в DXF для AutoCAD: стили, подписи и атрибуты объектов в блоках")
        self.action.triggered.connect(self.run)
        altan_toolbar.add_action(self.iface, self.action)
        altan_toolbar.add_to_menu(self.iface, self.action)

    def unload(self):
        if self.action:
            altan_toolbar.remove_from_menu(self.iface, self.action)
            altan_toolbar.remove_action(self.iface, self.action)
            self.action.deleteLater()
            self.action = None
        if self.provider:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None

    def run(self):
        project = QgsProject.instance()
        layers = visible_vector_layers(project)
        if not layers:
            self.iface.messageBar().pushMessage(
                "DXF", "В проекте нет включённых векторных слоёв", Qgis.MessageLevel.Warning, 5)
        import processing
        processing.execAlgorithmDialog(ALGORITHM_ID, {
            "LAYERS": [QgsProcessingParameterDxfLayers.layerAsVariantMap(QgsDxfExport.DxfLayer(lyr))
                       for lyr in layers],
            "CRS": project.crs(),
        })
