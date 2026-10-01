"""Чтение DXF: атрибуты блоков, которых не отдаёт GDAL.

GDAL разворачивает вставки блоков (INSERT) в их фигуры и отдаёт значения
атрибутов отдельными подписями, теряя сами имена атрибутов (NAME, NOTE…).
Зато фигурам он оставляет дескриптор вставки (поле EntityHandle), поэтому
значения можно вернуть на место: здесь файл разбирается по парам «код —
значение», и для каждой вставки собирается список «тег — значение».

Модуль не зависит от QGIS: на вход — путь к файлу.
"""

import codecs

from .dxf_blocks import python_codec

# Длинное значение атрибута пишется кусками: код 3 — начало, код 1 — конец.
_CHUNK_CODE = "3"
_VALUE_CODE = "1"
_TAG_CODE = "2"
_HANDLE_CODE = "5"
_LAYER_CODE = "8"
_NAME_CODE = "2"


class InsertInfo:
    """Вставка блока: имя блока и её атрибуты по порядку."""

    def __init__(self, name, layer):
        self.name = name  # имя блока
        self.layer = layer  # слой чертежа
        self.values = []  # [(тег, значение)] в порядке записи


class ReadResult:
    def __init__(self):
        self.inserts = {}  # дескриптор INSERT → InsertInfo
        self.attrib_handles = set()  # дескрипторы ATTRIB — это значения, не подписи
        self.label_handles = set()  # дескрипторы TEXT и MTEXT — подписи
        self.encoding = None  # чем файл оказался записан


BINARY_SENTINEL = b"AutoCAD Binary DXF"
CHUNK = 1 << 20  # по мегабайту за раз
# Единичные сбои UTF-8 прощаются только в файле, где много нерусского ASCII
# текста: иначе на чертеже с тремя русскими буквами CP1251 сойдёт за UTF-8.
MIN_NON_ASCII = 1000
MAX_BROKEN_SHARE = 0.001


def file_kind(path):
    """Что это за файл: «dxf», «dxf-binary», «dwg» или «unknown»."""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
    except OSError:  # папка, нет прав, файл исчез — разберётся вызывающий
        return "unknown"
    if head.startswith(BINARY_SENTINEL):
        return "dxf-binary"
    if head[:2] == b"AC" and head[2:6].isdigit():
        return "dwg"  # AC1015, AC1032… — так начинается DWG
    return "dxf" if head.lstrip()[:1].isdigit() else "unknown"


def _pairs(path, codec):
    """Файл DXF → пары (код, значение), построчно, не загружая его целиком."""
    with open(path, "r", encoding=codec, errors="replace") as f:
        while True:
            code = f.readline()
            if not code:
                return
            value = f.readline()
            if not value:
                return
            yield code.strip(), value.rstrip("\n")


def detect_encoding(path, declared="CP1251"):
    """Какой кодировкой записан текст в файле.

    Конвертеры DWG→DXF (dwg2dxf) пишут UTF-8, оставляя в заголовке
    $DWGCODEPAGE прежнюю кодировку: читатель по заголовку даёт кракозябры.
    Поэтому кодировка определяется по содержимому, а не по заголовку.

    Единичные сбои допускаются: длинное значение DXF режется на куски по
    числу байтов, и разрыв попадает в середину двухбайтового символа.
    Файл читается кусками — чертежи бывают в сотни мегабайт.
    """
    if file_kind(path) != "dxf":
        return declared
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    plain = bytes(range(128))
    non_ascii = broken = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            non_ascii += len(chunk.translate(None, plain))
            broken += decoder.decode(chunk).count("\ufffd")
    broken += decoder.decode(b"", True).count("\ufffd")
    if non_ascii == 0:
        return declared
    if broken == 0:
        return "UTF-8"
    if non_ascii >= MIN_NON_ASCII and broken <= non_ascii * MAX_BROKEN_SHARE:
        return "UTF-8"  # разрыв длинного значения посреди двухбайтового символа
    return declared


def declared_codepage(path):
    """Кодировка, объявленная в заголовке файла ($DWGCODEPAGE), или None."""
    try:
        with open(path, "rb") as f:
            head = f.read(16384).decode("ascii", errors="replace")
    except OSError:
        return None
    lines = head.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == "$DWGCODEPAGE" and i + 2 < len(lines):
            return lines[i + 2].strip() or None
    return None


def insert_attributes(path, encoding="CP1251"):
    """Атрибуты вставок блоков в пространстве модели.

    Отдаёт ReadResult: по дескриптору вставки — её блок и значения атрибутов.
    Разбираются только сущности раздела ENTITIES: описания блоков (раздел
    BLOCKS) значений не содержат. У двоичного DXF атрибуты не читаются —
    результат пустой.
    """
    result = ReadResult()
    if file_kind(path) != "dxf":
        return result
    codec = python_codec(encoding)
    result.encoding = codec
    in_entities = False
    section = False  # следующий код 2 — имя раздела
    kind = None  # разбираемая сейчас сущность
    tags = []  # её пары
    current = None  # дескриптор вставки, к которой относятся ATTRIB

    def finish():
        """Закончить разбор сущности: INSERT запомнить, ATTRIB отдать вставке."""
        if kind == "INSERT":
            handle = _first(tags, _HANDLE_CODE)
            if handle:
                result.inserts[handle] = InsertInfo(_first(tags, _NAME_CODE, ""),
                                                    _first(tags, _LAYER_CODE, "0"))
                return handle
            return None
        if kind == "ATTRIB":
            handle = _first(tags, _HANDLE_CODE)
            if handle:
                result.attrib_handles.add(handle)
            info = result.inserts.get(current)
            if info is not None:
                tag = _first(tags, _TAG_CODE, "")
                if tag:
                    info.values.append((tag, _attrib_value(tags)))
            return current
        if kind in ("TEXT", "MTEXT"):
            handle = _first(tags, _HANDLE_CODE)
            if handle:
                result.label_handles.add(handle)
            return current
        if kind == "SEQEND":
            return None
        return current

    for code, value in _pairs(path, codec):
        if code == "0":
            if kind is not None and in_entities:
                current = finish()
            kind = value.strip()
            tags = []
            if kind == "SECTION":
                section = True
            elif kind == "ENDSEC":
                if in_entities:
                    break
                in_entities = False
            continue
        if section and code == "2":
            in_entities = value.strip() == "ENTITIES"
            section = False
            continue
        tags.append((code, value))
    if kind is not None and in_entities:
        finish()
    return result


def _first(tags, code, default=None):
    for c, v in tags:
        if c == code:
            return v.strip() if code != _VALUE_CODE else v
    return default


def _attrib_value(tags):
    """Значение атрибута: куски кода 3 плюс код 1."""
    chunks = [v for c, v in tags if c == _CHUNK_CODE]
    last = [v for c, v in tags if c == _VALUE_CODE]
    return "".join(chunks) + (last[0] if last else "")
