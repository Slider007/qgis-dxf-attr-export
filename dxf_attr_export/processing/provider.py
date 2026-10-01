import os

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .export_dxf import ExportDxfAlgorithm
from .import_dxf import ImportDxfAlgorithm

PLUGIN_DIR = os.path.dirname(os.path.dirname(__file__))


class DxfAttrExportProvider(QgsProcessingProvider):
    def id(self):
        return "dxfattrexport"

    def name(self):
        return "DXF с атрибутами"

    def icon(self):
        return QIcon(os.path.join(PLUGIN_DIR, "icon.svg"))

    def loadAlgorithms(self):
        self.addAlgorithm(ExportDxfAlgorithm())
        self.addAlgorithm(ImportDxfAlgorithm())
