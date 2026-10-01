import os

from qgis.core import Qgis, QgsApplication, QgsDxfExport, QgsProcessingParameterDxfLayers, QgsProject, QgsVectorLayer
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QMenu, QToolButton

from . import altan_toolbar
from .processing.provider import DxfAttrExportProvider

try:  # Qt6 / QGIS 4: QAction живёт в QtGui
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 / QGIS 3
    from qgis.PyQt.QtWidgets import QAction

PLUGIN_DIR = os.path.dirname(__file__)
ALGORITHM_ID = "dxfattrexport:exportdxf"
IMPORT_ALGORITHM_ID = "dxfattrexport:importdxf"


def visible_vector_layers(project):
    """Включённые в дереве слоёв векторные слои с геометрией, в порядке дерева."""
    return [lyr for lyr in project.layerTreeRoot().checkedLayers()
            if isinstance(lyr, QgsVectorLayer) and lyr.isSpatial()]


class DxfAttrExportPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None  # кнопка на панели: открывает список из двух действий
        self.export_action = None
        self.import_action = None
        self.menu = None
        self.provider = None

    def initProcessing(self):
        self.provider = DxfAttrExportProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        self.initProcessing()
        icon = QIcon(os.path.join(PLUGIN_DIR, "icon.svg"))
        window = self.iface.mainWindow()
        self.export_action = QAction(icon, "Экспорт в DXF с атрибутами…", window)
        self.export_action.setToolTip(
            "Выгрузить слои в DXF для AutoCAD: стили, подписи и атрибуты объектов в блоках")
        self.export_action.triggered.connect(self.run)
        self.import_action = QAction(icon, "Импорт DXF в ГИС-формат…", window)
        self.import_action.setToolTip(
            "Прочитать чертёж DXF: слои AutoCAD — отдельными слоями, атрибуты блоков — полями")
        self.import_action.triggered.connect(self.run_import)

        # одна кнопка на общей панели: нажатие открывает список из двух действий
        self.menu = QMenu(window)
        self.menu.addAction(self.export_action)
        self.menu.addAction(self.import_action)
        self.action = QAction(icon, "DXF с атрибутами", window)
        self.action.setToolTip("Выгрузка слоёв в DXF для AutoCAD и чтение чертежей DXF обратно")
        self.action.setMenu(self.menu)  # до добавления на панель: иначе не будет стрелки
        bar = altan_toolbar.add_action(self.iface, self.action)
        button = bar.widgetForAction(self.action)
        if isinstance(button, QToolButton):
            button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        for action in (self.export_action, self.import_action):
            altan_toolbar.add_to_menu(self.iface, action)

    def unload(self):
        for name in ("export_action", "import_action"):
            action = getattr(self, name)
            if action:
                altan_toolbar.remove_from_menu(self.iface, action)
                action.deleteLater()
                setattr(self, name, None)
        if self.action:
            altan_toolbar.remove_action(self.iface, self.action)
            self.action.setMenu(None)
            self.action.deleteLater()
            self.action = None
        if self.menu:
            self.menu.deleteLater()
            self.menu = None
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

    def run_import(self):
        import processing
        processing.execAlgorithmDialog(IMPORT_ALGORITHM_ID, {"CRS": QgsProject.instance().crs()})
