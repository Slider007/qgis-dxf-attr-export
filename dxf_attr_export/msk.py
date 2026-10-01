"""Встроенный набор МСК и выбор МСК по месту. Без GUI."""

import json
import os
import re

from qgis.core import (
    QgsApplication,
    QgsBlockingNetworkRequest,
    QgsCoordinateReferenceSystem,
)
from qgis.PyQt.QtCore import QUrl, QUrlQuery
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest

DATA = os.path.join(os.path.dirname(__file__), "msk.json")
NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "QGIS plugin project_utm_crs (https://github.com/Slider007/qgis-project-utm-crs)"
TIMEOUT_MS = 10000
# Файл копируется в другие модули, поэтому текст без действий: кнопки у них свои,
# что делать дальше — дописывает окно модуля.
NO_ANSWER = "Нет связи с сервисом адресов OpenStreetMap."

# Код субъекта по ISO 3166-2:RU (так его отдаёт Nominatim в ISO3166-2-lvl4)
ISO_TO_CODE = {
    "AD": 1, "BA": 2, "BU": 3, "AL": 4, "DA": 5, "IN": 6, "KB": 7, "KL": 8,
    "KC": 9, "KR": 10, "KO": 11, "ME": 12, "MO": 13, "SA": 14, "SE": 15,
    "TA": 16, "TY": 17, "UD": 18, "KK": 19, "CE": 20, "CU": 21, "ALT": 22,
    "KDA": 23, "KYA": 24, "PRI": 25, "STA": 26, "KHA": 27, "AMU": 28,
    "ARK": 29, "AST": 30, "BEL": 31, "BRY": 32, "VLA": 33, "VGG": 34,
    "VLG": 35, "VOR": 36, "IVA": 37, "IRK": 38, "KGD": 39, "KLU": 40,
    "KAM": 41, "KEM": 42, "KIR": 43, "KOS": 44, "KGN": 45, "KRS": 46,
    "LEN": 47, "LIP": 48, "MAG": 49, "MOS": 50, "MUR": 51, "NIZ": 52,
    "NGR": 53, "NVS": 54, "OMS": 55, "ORE": 56, "ORL": 57, "PNZ": 58,
    "PER": 59, "PSK": 60, "ROS": 61, "RYA": 62, "SAM": 63, "SAR": 64,
    "SAK": 65, "SVE": 66, "SMO": 67, "TAM": 68, "TVE": 69, "TOM": 70,
    "TUL": 71, "TYU": 72, "ULY": 73, "CHE": 74, "ZAB": 75, "YAR": 76,
    "MOW": 77, "SPE": 78, "YEV": 79, "NEN": 83, "KHM": 86, "CHU": 87,
    "YAN": 89,
}

_items = None


def items():
    """Все МСК набора: словари name, code, subject, zone, variant, proj."""
    global _items
    if _items is None:
        with open(DATA, encoding="utf-8") as f:
            _items = json.load(f)
    return _items


def central_meridian(item):
    m = re.search(r"\+lon_0=(-?[\d.]+)", item["proj"])
    return float(m.group(1)) if m else None


def short_name(item):
    """«МСК-50 зона 1» из «МСК-50 зона 1 Московская область»."""
    name = item["name"]
    subject = item["subject"]
    if subject and name.endswith(subject):
        name = name[: -len(subject)]
    return name.strip()


# ------------------------------------------------------------ субъект по месту

def region_from_reverse(answer):
    """(код субъекта, название) из ответа Nominatim reverse (jsonv2)."""
    address = (answer or {}).get("address") or {}
    if address.get("country_code") != "ru":
        return None, address.get("country") or ""
    iso = address.get("ISO3166-2-lvl4") or ""
    name = address.get("state") or address.get("region") or ""
    code = ISO_TO_CODE.get(iso[3:]) if iso.startswith("RU-") else None
    if code is None and name:
        # запасной путь: по названию субъекта в наборе
        key = _norm(name)
        for item in items():
            if _norm(item["subject"]) == key:
                code = item["code"]
                break
    return code, name


def _norm(text):
    return re.sub(r"[^а-яёa-z0-9]", "", text.lower().replace("ё", "е"))


def _request(lon, lat):
    """Запрос к Nominatim с собственным сроком ожидания."""
    url = QUrl(NOMINATIM)
    query = QUrlQuery()
    for key, value in (("lat", "%.6f" % lat), ("lon", "%.6f" % lon), ("format", "jsonv2"),
                       ("zoom", "5"), ("accept-language", "ru")):
        query.addQueryItem(key, value)
    url.setQuery(query)
    request = QNetworkRequest(url)
    request.setRawHeader(b"User-Agent", USER_AGENT.encode())
    # свой срок: общий таймаут сети QGIS — 60 с, столько ждать ответа о субъекте незачем
    request.setTransferTimeout(TIMEOUT_MS)
    return request


def _answer_from_reply(reply):
    """(ответ, ошибка) из ответа сети.

    При истёкшем сроке QgsBlockingNetworkRequest возвращает NoError, а в самом ответе
    стоит ошибка и пустое тело: без этой проверки человек читал «непонятный ответ»
    вместо «нет связи».
    """
    if reply.error() != QNetworkReply.NetworkError.NoError:
        return None, NO_ANSWER
    body = bytes(reply.content()).strip()
    if not body:
        return None, NO_ANSWER
    try:
        return json.loads(body.decode("utf-8")), None
    except ValueError:
        return None, "Сервис адресов OpenStreetMap ответил непонятно."


def reverse_geocode(lon, lat):
    """Запрос к Nominatim. Возвращает (ответ, None) или (None, текст ошибки)."""
    blocking = QgsBlockingNetworkRequest()
    if blocking.get(_request(lon, lat)) != QgsBlockingNetworkRequest.ErrorCode.NoError:
        return None, NO_ANSWER
    return _answer_from_reply(blocking.reply())


def zones_for(code, lon):
    """МСК субъекта: сначала основной вариант, внутри — по близости осевого меридиана."""
    found = [i for i in items() if i["code"] == code]

    def key(item):
        cm = central_meridian(item)
        return (item["variant"] != "", item["variant"],
                abs(lon - cm) if cm is not None else 999)
    return sorted(found, key=key)


# ------------------------------------------------------------ СК QGIS

def _norm_proj(proj):
    return " ".join(p for p in (proj or "").split() if p != "+type=crs")


def item_for_crs(crs):
    """Запись набора для СК QGIS (в том числе пользовательской СК из набора) или None."""
    key = _norm_proj(crs.toProj())
    for item in items():
        if _norm_proj(item["proj"]) == key:
            return item
    return None


def crs_for(item, register=True):
    """СК QGIS для записи набора.

    Если такая СК уже есть среди пользовательских, берётся она. Иначе при
    register=True добавляется в «Пользовательские СК» под своим названием:
    так название видно в QGIS и сохраняется в проекте."""
    registry = QgsApplication.coordinateReferenceSystemRegistry()
    target = _norm_proj(item["proj"])
    for details in registry.userCrsList():
        if _norm_proj(details.proj) == target:
            return QgsCoordinateReferenceSystem("USER:%d" % details.id)
    crs = QgsCoordinateReferenceSystem.fromProj(item["proj"])
    if register and crs.isValid():
        srs_id = registry.addUserCrs(crs, item["name"])
        if srs_id >= 0:
            return QgsCoordinateReferenceSystem("USER:%d" % srs_id)
    return crs


# ------------------------------------------------------------ СК-63

# Зоны СК-63 есть в EPSG: код, район и зона, осевой меридиан (из базы PROJ).
# Свои параметры не храним — СК берётся по коду EPSG.
CS63 = (
    (2935, "A1", 41.53333333), (2936, "A2", 44.53333333),
    (2937, "A3", 47.53333333), (2938, "A4", 50.53333333),
    (3350, "C0", 21.95), (3351, "C1", 24.95), (3352, "C2", 27.95),
    (2939, "K2", 50.76666667), (2940, "K3", 53.76666667), (2941, "K4", 56.76666667),
    (7825, "X1", 23.5), (7826, "X2", 26.5), (7827, "X3", 29.5), (7828, "X4", 32.5),
    (7829, "X5", 35.5), (7830, "X6", 38.5), (7831, "X7", 41.5),
)
# Зона шириной 3°: берём свою и соседние
CS63_MAX_DELTA = 4.5


def cs63_near(lon, max_delta=CS63_MAX_DELTA):
    """Зоны СК-63 рядом с долготой: (код EPSG, «район и зона», осевой меридиан)."""
    found = [z for z in CS63 if abs(lon - z[2]) <= max_delta]
    return sorted(found, key=lambda z: abs(lon - z[2]))


def cs63_name(zone):
    """«СК-63 район X зона 4 (EPSG:7828)»."""
    code, label, _ = zone
    return "СК-63 район {} зона {} (EPSG:{})".format(label[0], label[1], code)


def cs63_crs(zone):
    return QgsCoordinateReferenceSystem("EPSG:%d" % zone[0])
