import os

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCsException,
    QgsDxfExport,
    QgsNetworkAccessManager,
    QgsProcessingParameterDxfLayers,
    QgsProject,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QCursor, QIcon
from qgis.PyQt.QtWidgets import QApplication, QMenu, QToolButton

from . import altan_toolbar, msk
from .processing.provider import DxfAttrExportProvider

try:  # Qt6 / QGIS 4: QAction живёт в QtGui
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 / QGIS 3
    from qgis.PyQt.QtWidgets import QAction

PLUGIN_DIR = os.path.dirname(__file__)
# msk.py и msk.json — копии из модуля project_utm_crs, правятся там.
# Представляемся сервису адресов своим именем, файл при этом не меняем.
msk.USER_AGENT = "QGIS plugin dxf_attr_export (https://github.com/Slider007/qgis-dxf-attr-export)"
# Сколько ждём сервис адресов: QGIS по умолчанию ждёт минуту, а окно за это
# время не должно висеть. Не ответил — откроемся с СК проекта.
LOOKUP_TIMEOUT = 5000
ALGORITHM_ID = "dxfattrexport:exportdxf"
IMPORT_ALGORITHM_ID = "dxfattrexport:importdxf"


def lonlat(point, crs, context):
    """Точка в градусы (долгота, широта); None — пересчитать не удалось."""
    wgs = QgsCoordinateReferenceSystem("EPSG:4326")
    if not crs.isValid() or not wgs.isValid():
        return None
    try:
        out = QgsCoordinateTransform(crs, wgs, context).transform(point)
    except QgsCsException:
        return None
    return out.x(), out.y()


# СК, с которыми чертёж не сделать: градусы и веб-Меркатор (с ним открывают подложки)
ROUGH_CRS = ("EPSG:3857", "EPSG:900913")


def needs_msk(crs):
    """У проекта СК, непригодная для чертежа?"""
    return not crs.isValid() or crs.isGeographic() or crs.authid() in ROUGH_CRS


def drawing_crs(canvas, project):
    """СК чертежа: МСК по центру карты, если у проекта её нет.

    Чертежи AutoCAD почти всегда в МСК. Если у проекта уже задана метровая СК —
    это осознанный выбор, его не трогаем. Отдаёт (СК, название МСК или None,
    сообщение о неудаче или None).
    """
    current = project.crs()
    if not needs_msk(current):
        return current, None, None
    # по пустому проекту место не определить: карта смотрит в никуда,
    # а зря потраченное ожидание сети человек видит как подвисание
    if canvas is None or not project.mapLayers() or canvas.extent().isEmpty():
        return current, None, None
    place = lonlat(canvas.extent().center(), canvas.mapSettings().destinationCrs(),
                   project.transformContext())
    if place is None:
        return current, None, None
    lon, lat = place
    was = QgsNetworkAccessManager.timeout()
    QgsNetworkAccessManager.setTimeout(LOOKUP_TIMEOUT)
    try:
        answer, error = msk.reverse_geocode(lon, lat)
    finally:
        QgsNetworkAccessManager.setTimeout(was)
    if error:
        return current, None, error
    code, name = msk.region_from_reverse(answer)
    if code is None:
        return current, None, None
    zones = msk.zones_for(code, lon)
    if not zones:
        return current, None, "Для места «{}» в модуле нет МСК.".format(name)
    found = msk.crs_for(zones[0])
    if not found.isValid():
        return current, None, None
    return found, zones[0]["name"], None


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
            self.message("В проекте нет включённых векторных слоёв", Qgis.MessageLevel.Warning)
        crs, name, problem = self.ask_crs()
        self.say(name and "Координаты DXF будут записаны в «{}» — определено по центру карты, "
                          "СК добавлена в «Пользовательские СК» QGIS. Другую можно выбрать "
                          "в окне.".format(name), problem)
        import processing
        processing.execAlgorithmDialog(ALGORITHM_ID, {
            "LAYERS": [QgsProcessingParameterDxfLayers.layerAsVariantMap(QgsDxfExport.DxfLayer(lyr))
                       for lyr in layers],
            "CRS": crs,
        })

    def ask_crs(self):
        """СК для окна. Запрос уходит в сеть, поэтому показываем песочные часы."""
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            return drawing_crs(self.iface.mapCanvas(), QgsProject.instance())
        finally:
            QApplication.restoreOverrideCursor()

    def message(self, text, level=Qgis.MessageLevel.Info, seconds=10):
        """Сообщение в строке QGIS; без строки сообщений (в проверках) — молча."""
        bar = self.iface.messageBar()
        if bar is not None:
            bar.pushMessage("DXF", text, level, seconds)

    def say(self, note, problem):
        """Сказать, какая СК подставлена или почему не вышло."""
        if problem:
            self.message(problem + " Система координат — как у проекта.",
                         Qgis.MessageLevel.Warning)
        elif note:
            self.message(note)

    def run_import(self):
        project = QgsProject.instance()
        crs, name, problem = self.ask_crs()
        self.say(name and "Система координат чертежа: «{}» — определено по центру карты, "
                          "СК добавлена в «Пользовательские СК» QGIS. Если чертёж в другой, "
                          "выберите её в окне.".format(name), problem)
        import processing
        processing.execAlgorithmDialog(IMPORT_ALGORITHM_ID, {"CRS": crs})
