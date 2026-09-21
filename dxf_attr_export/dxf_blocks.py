"""Доработка DXF, записанного QgsDxfExport: объекты становятся блоками с атрибутами.

QgsDxfExport не пишет атрибуты. Поэтому экспорт идёт с временным именем слоя
DXF для каждого объекта (например, «QFX_12»), а здесь файл разбирается и:

- геометрия объекта переносится в собственный блок, в блоке — описания
  атрибутов (ATTDEF), в пространстве модели — вставка блока (INSERT) со
  значениями атрибутов (ATTRIB) на месте первой фигуры объекта;
- подписи (TEXT, MTEXT) остаются в пространстве модели;
- временные слои заменяются настоящими (имя слоя QGIS или значение поля).

Модуль не зависит от QGIS: на вход — путь к файлу и описание объектов.
"""

import codecs

# Символы, недопустимые в именах слоёв и блоков AutoCAD
_BAD_NAME_CHARS = set('<>/\\":;?*|=`,')
MAX_VALUE_LEN = 250

# Сущности, которые пишутся вслед за основной и принадлежат ей
_SUB_ENTITIES = {"VERTEX", "SEQEND", "ATTRIB"}
_LABEL_ENTITIES = {"TEXT", "MTEXT"}


class FeatureBlock:
    """Что известно об объекте: временный слой, настоящий слой, точка вставки, атрибуты."""

    def __init__(self, key, layer, anchor, values, shapes=None, color=None):
        self.key = key  # временное имя слоя DXF, по которому находятся фигуры объекта
        self.layer = layer  # настоящий слой DXF
        self.anchor = anchor  # (x, y) — точка вставки блока; None — у объекта нет геометрии
        self.values = values  # список значений, по порядку полей
        # Только границы: фигуры QGIS отбрасываются, в блок пишутся эти.
        # [("POINT", [(x, y)], False) | ("LINE", [(x, y), …], замкнута)]; None — оформление QGIS
        self.shapes = shapes
        self.color = color  # (r, g, b) для shapes; None — цвет слоя
        # Фигуры, которые дописываются в блок поверх остального (линии штриховки):
        # [(вид, точки, замкнута, (r, g, b))]
        self.extra = []
        # Штриховки AutoCAD: [((r, g, b), угол в градусах от оси X, шаг,
        # [(точки контура, внешний ли контур)])]
        self.hatches = []


class ExportLayer:
    """Слой QGIS: поля (имя, подсказка) и его объекты."""

    def __init__(self, fields, features):
        self.fields = fields  # [(имя поля, подсказка)]
        self.features = features  # [FeatureBlock]


class Result:
    def __init__(self):
        self.blocks = 0  # объектов записано блоками
        self.without_geometry = 0  # объектов, у которых в DXF не оказалось фигур
        self.truncated = 0  # значений, укороченных до MAX_VALUE_LEN


def safe_name(name, fallback="0"):
    """Имя слоя или блока, допустимое в AutoCAD."""
    out = "".join("_" if (c in _BAD_NAME_CHARS or ord(c) < 32) else c for c in str(name))
    out = out.strip()[:255]
    return out or fallback


def attribute_tags(names):
    """Теги атрибутов: без пробелов, в верхнем регистре, без повторов."""
    tags, seen = [], set()
    for name in names:
        tag = "".join("_" if (c.isspace() or c in _BAD_NAME_CHARS or ord(c) < 32) else c
                      for c in str(name)).upper()[:250] or "FIELD"
        base, n = tag, 2
        while tag in seen:
            tag = "{}_{}".format(base, n)
            n += 1
        seen.add(tag)
        tags.append(tag)
    return tags


def read_codec(data, encoding):
    """Кодировка, которой файл записан на самом деле.

    QGIS 3 пишет байты в выбранной кодировке, как и указано в заголовке
    $DWGCODEPAGE. QGIS 4 (Qt6) при том же заголовке пишет UTF-8 — такой файл
    читатели DXF показывают кракозябрами. Файл распознаётся по содержимому,
    а записывается обратно уже в объявленной кодировке.
    """
    if not data.isascii():
        try:
            data.decode("utf-8")
            return "utf-8"
        except UnicodeDecodeError:
            pass
    return python_codec(encoding)


def python_codec(encoding):
    """Имя кодировки QGIS (CP1251, MacRoman, Shift_JIS…) → кодек Python."""
    for name in (encoding, encoding.replace("_", "-"), {"macroman": "mac_roman"}.get(encoding.lower(), "")):
        try:
            return codecs.lookup(name).name
        except (LookupError, TypeError):
            pass
    raise ValueError("Неизвестная кодировка: {}".format(encoding))


def _fmt(v):
    return repr(float(v))


class _Entity:
    """Сущность DXF — список пар (код, значение), код строкой без пробелов."""

    def __init__(self, tags):
        self.tags = tags

    @property
    def kind(self):
        return self.tags[0][1].strip()

    def get(self, code, default=None):
        for c, v in self.tags:
            if c == code:
                return v
        return default

    def set(self, code, value, after=None):
        """Заменить первое значение кода; если его нет — вставить после кода `after`."""
        for i, (c, _) in enumerate(self.tags):
            if c == code:
                self.tags[i] = (code, value)
                return
        pos = 1
        if after is not None:
            for i, (c, _) in enumerate(self.tags):
                if c == after:
                    pos = i + 1
                    break
        self.tags.insert(pos, (code, value))


def _split(tags):
    """Пары → список сущностей (каждая начинается с кода 0)."""
    out = []
    for c, v in tags:
        if c == "0" or not out:
            out.append(_Entity([]))
        out[-1].tags.append((c, v))
    return out


def _join(entities):
    return [t for e in entities for t in e.tags]


class _Doc:
    def __init__(self, text):
        self.newline = "\r\n" if "\r\n" in text[:4096] else "\n"
        lines = text.split("\n")
        if lines and lines[-1].strip() == "":
            lines.pop()
        lines = [ln.rstrip("\r") for ln in lines]
        if len(lines) % 2:
            raise ValueError("Файл DXF повреждён: нечётное число строк")
        tags = [(lines[i].strip(), lines[i + 1]) for i in range(0, len(lines), 2)]
        # Разбиение на секции: [(имя, [пары внутри секции])], хвост (EOF)
        self.sections = []
        self.tail = []
        i = 0
        while i < len(tags):
            c, v = tags[i]
            if c == "0" and v.strip() == "SECTION":
                name = tags[i + 1][1].strip()
                j = i + 2
                while not (tags[j][0] == "0" and tags[j][1].strip() == "ENDSEC"):
                    j += 1
                self.sections.append((name, tags[i + 2:j]))
                i = j + 1
            else:
                self.tail.append((c, v))
                i += 1
        # $HANDSEED в заголовке тоже с кодом 5 — его не считаем
        handles = [int(v.strip(), 16) for name, stags in self.sections if name != "HEADER"
                   for c, v in stags if c in ("5", "105")]
        self.next_handle = max(handles) + 1 if handles else 0x100

    def section(self, name):
        for n, tags in self.sections:
            if n == name:
                return tags
        return None

    def set_section(self, name, tags):
        for i, (n, _) in enumerate(self.sections):
            if n == name:
                self.sections[i] = (n, tags)
                return
        raise ValueError("В DXF нет секции " + name)

    def handle(self):
        h = "{:X}".format(self.next_handle)
        self.next_handle += 1
        return h

    def text(self):
        out = []
        for name, tags in self.sections:
            out += [("0", "SECTION"), ("2", name)] + tags + [("0", "ENDSEC")]
        out += self.tail
        nl = self.newline
        return nl.join("{:>3}{}{}".format(c, nl, v) for c, v in out) + nl


def _header_set(tags, var, code, value):
    """Задать переменную заголовка ($HANDSEED и т.п.), добавив её при отсутствии."""
    for i, (c, v) in enumerate(tags):
        if c == "9" and v.strip() == var:
            tags[i + 1] = (code, value)
            return
    tags += [("9", var), (code, value)]


def _table(tables, name):
    """(индекс начала, индекс конца) сущностей таблицы в списке сущностей TABLES."""
    start = None
    for i, e in enumerate(tables):
        if e.kind == "TABLE" and (e.get("2") or "").strip() == name:
            start = i
        elif start is not None and e.kind == "ENDTAB":
            return start, i
    raise ValueError("В DXF нет таблицы " + name)


def _shape_entities(doc, rec_h, lname, items, default_color=None):
    """Фигуры (точки и полилинии) как сущности DXF: [(вид, точки, замкнута[, цвет])]."""
    out = []
    for item in items:
        kind, points, closed = item[0], item[1], item[2]
        rgb = item[3] if len(item) > 3 else default_color
        head = [("5", doc.handle()), ("330", rec_h), ("100", "AcDbEntity"), ("8", lname)]
        if rgb:
            head.append(("420", "{:>9}".format((rgb[0] << 16) | (rgb[1] << 8) | rgb[2])))
        if kind == "POINT":
            out.append(_Entity([("0", "POINT")] + head + [
                ("100", "AcDbPoint"), ("10", _fmt(points[0][0])), ("20", _fmt(points[0][1])),
                ("30", "0.0")]))
        else:
            tags = [("0", "LWPOLYLINE")] + head + [
                ("100", "AcDbPolyline"), ("90", "{:>6}".format(len(points))),
                ("70", "     1" if closed else "     0"), ("43", "0.0")]
            for px, py in points:
                tags += [("10", _fmt(px)), ("20", _fmt(py))]
            out.append(_Entity(tags))
    return out


def _hatch_entities(doc, rec_h, lname, hatches):
    """Штриховка AutoCAD: узор задан в самом файле (одна линия под углом с шагом)."""
    out = []
    for rgb, angle, spacing, rings in hatches:
        rings = [r for r in rings if len(r[0]) > 2]
        if not rings or spacing <= 0:
            continue
        tags = [("0", "HATCH"), ("5", doc.handle()), ("330", rec_h), ("100", "AcDbEntity"),
                ("8", lname)]
        if rgb:
            tags.append(("420", "{:>9}".format((rgb[0] << 16) | (rgb[1] << 8) | rgb[2])))
        tags += [("100", "AcDbHatch"), ("10", "0.0"), ("20", "0.0"), ("30", "0.0"),
                 ("210", "0.0"), ("220", "0.0"), ("230", "1.0"),
                 ("2", "_USER"),  # узор не из acad.pat, а описан ниже
                 ("70", "     0"), ("71", "     0"), ("91", "{:>6}".format(len(rings)))]
        for points, external in rings:
            tags += [("92", "     3" if external else "     2"), ("72", "     0"),
                     ("73", "     1"), ("93", "{:>6}".format(len(points)))]
            for px, py in points:
                tags += [("10", _fmt(px)), ("20", _fmt(py))]
            tags.append(("97", "     0"))
        tags += [("75", "     0"), ("76", "     0"), ("52", "0.0"), ("41", "1.0"),
                 ("77", "     0"), ("78", "     1"),
                 ("53", _fmt(angle)), ("43", "0.0"), ("44", "0.0"),
                 ("45", "0.0"), ("46", _fmt(spacing)), ("79", "     0"), ("98", "     0")]
        out.append(_Entity(tags))
    return out


def _encoder(codec):
    def enc(s):
        out = []
        for ch in s:
            try:
                ch.encode(codec)
                out.append(ch)
            except UnicodeEncodeError:
                out.append("\\U+{:04X}".format(ord(ch)) if ord(ch) <= 0xFFFF else "?")
        return "".join(out)
    return enc


def text_value(value, result=None):
    """Значение атрибута одной строкой без управляющих символов, не длиннее MAX_VALUE_LEN."""
    s = "" if value is None else str(value)
    s = " ".join(s.replace("\r", " ").replace("\n", " ").replace("\t", " ").split(" "))
    s = "".join(c for c in s if ord(c) >= 32).replace("^", "^ ")
    if len(s) > MAX_VALUE_LEN:
        s = s[:MAX_VALUE_LEN]
        if result is not None:
            result.truncated += 1
    return s


def add_attribute_blocks(path, encoding, layers, text_height=2.5, visible=False, insunits=None):
    """Переписать DXF по пути `path`: объекты из `layers` становятся блоками с атрибутами.

    `layers` — список ExportLayer; `insunits` — код единиц чертежа AutoCAD
    ($INSUNITS: 6 — метры). Возвращает Result.
    """
    with open(path, "rb") as f:
        data = f.read()
    codec = python_codec(encoding)  # в этой кодировке файл будет записан
    enc = _encoder(codec)
    doc = _Doc(data.decode(read_codec(data, encoding), errors="replace"))
    result = Result()

    by_key = {}
    for lay in layers:
        for fb in lay.features:
            by_key[fb.key] = (lay, fb)

    # --- таблицы: слои и записи блоков
    tables = _split(doc.section("TABLES"))
    ls, le = _table(tables, "LAYER")
    layer_records = tables[ls + 1:le]
    template = next((e for e in layer_records if (e.get("2") or "").strip() == "0"), None)
    kept, existing = [], {}
    for e in layer_records:
        name = (e.get("2") or "").strip()
        if name in by_key:
            continue
        kept.append(e)
        existing[name.casefold()] = name
    # Настоящие слои: одно написание на слой (AutoCAD не различает регистр)
    for _, fb in by_key.values():
        fb.layer = existing.setdefault(fb.layer.casefold(), fb.layer)
    new_layers = [n for n in dict.fromkeys(fb.layer for _, fb in by_key.values())
                  if n not in {(r.get("2") or "").strip() for r in kept}]
    for name in new_layers:
        if template is not None:
            rec = _Entity(list(template.tags))
            rec.set("5", doc.handle())
            rec.set("2", enc(name))
            rec.set("62", "     7")
        else:
            rec = _Entity([("0", "LAYER"), ("5", doc.handle()), ("100", "AcDbSymbolTableRecord"),
                           ("100", "AcDbLayerTableRecord"), ("2", enc(name)), ("70", "     0"),
                           ("62", "     7"), ("6", "CONTINUOUS")])
        kept.append(rec)
    tables[ls].set("70", "{:>6}".format(len(kept)))
    tables[ls + 1:le] = kept

    bs, be = _table(tables, "BLOCK_RECORD")
    block_table_handle = tables[bs].get("5").strip()
    block_names = {(e.get("2") or "").strip().casefold() for e in tables[bs + 1:be]}
    model_space = next((e.get("5").strip() for e in tables[bs + 1:be]
                        if (e.get("2") or "").strip().casefold() == "*model_space"), None)

    # --- сущности: фигуры объектов собираются, подписи переводятся на настоящий слой
    entities = _split(doc.section("ENTITIES"))
    units = []  # основная сущность + подчинённые (VERTEX, ATTRIB, SEQEND)
    for e in entities:
        if units and e.kind in _SUB_ENTITIES:
            units[-1].append(e)
        else:
            units.append([e])

    geometry = {}  # key → [unit]
    first_pos = {}  # key → номер первой фигуры в units
    out_units = []
    for unit in units:
        main = unit[0]
        key = (main.get("8") or "").strip()
        if key not in by_key:
            out_units.append(unit)
            continue
        layer_name = enc(by_key[key][1].layer)
        for e in unit:
            if e.get("8") is not None:
                e.set("8", layer_name)
        if main.kind in _LABEL_ENTITIES:
            out_units.append(unit)
            continue
        if key not in first_pos:
            first_pos[key] = len(out_units)
            out_units.append(None)  # сюда встанет вставка блока
        if by_key[key][1].shapes is None:
            geometry.setdefault(key, []).append(unit)

    # --- блоки
    blocks = _split(doc.section("BLOCKS"))
    for e in blocks:  # на случай, если временные слои попали в блоки символов
        key = (e.get("8") or "").strip()
        if key in by_key:
            e.set("8", enc(by_key[key][1].layer))

    flags = "     0" if visible else "     1"
    new_records, new_blocks = [], []
    counters = {}
    for lay in layers:
        tags = attribute_tags(n for n, _ in lay.fields)
        for fb in lay.features:
            content = geometry.get(fb.key) if fb.shapes is None else fb.shapes
            if not (content or fb.extra or fb.hatches) or fb.anchor is None:
                result.without_geometry += 1
                continue
            if fb.key not in first_pos:  # QGIS не нарисовал объект, а границы есть
                first_pos[fb.key] = len(out_units)
                out_units.append(None)
            base = safe_name(fb.layer)
            n = counters.get(base, 0)
            while True:
                n += 1
                bname = "{}_{}".format(base, n)
                if bname.casefold() not in block_names:
                    break
            counters[base] = n
            block_names.add(bname.casefold())
            bname = enc(bname)
            lname = enc(fb.layer)
            x, y = _fmt(fb.anchor[0]), _fmt(fb.anchor[1])

            rec_h = doc.handle()
            new_records.append(_Entity([
                ("0", "BLOCK_RECORD"), ("5", rec_h), ("330", block_table_handle),
                ("100", "AcDbSymbolTableRecord"), ("100", "AcDbBlockTableRecord"),
                ("2", bname), ("340", "0")]))

            body = [_Entity([
                ("0", "BLOCK"), ("5", doc.handle()), ("330", rec_h), ("100", "AcDbEntity"),
                ("8", lname), ("100", "AcDbBlockBegin"), ("2", bname),
                ("70", "     2" if tags else "     0"),
                ("10", x), ("20", y), ("30", "0.0"), ("3", bname), ("1", "")])]
            for i, (tag, (_, prompt)) in enumerate(zip(tags, lay.fields)):
                ty = _fmt(fb.anchor[1] - i * text_height * 1.5)
                body.append(_Entity([
                    ("0", "ATTDEF"), ("5", doc.handle()), ("330", rec_h), ("100", "AcDbEntity"),
                    ("8", lname), ("100", "AcDbText"), ("10", x), ("20", ty), ("30", "0.0"),
                    ("40", _fmt(text_height)), ("1", ""), ("100", "AcDbAttributeDefinition"),
                    ("3", enc(text_value(prompt))), ("2", enc(tag)), ("70", flags)]))
            if fb.shapes is None:
                for unit in (content or []):
                    unit[0].set("330", rec_h, after="5")
                    body += unit
            else:
                body += _shape_entities(doc, rec_h, lname, content, fb.color)
            body += _hatch_entities(doc, rec_h, lname, fb.hatches)
            body += _shape_entities(doc, rec_h, lname, fb.extra, fb.color)
            body.append(_Entity([
                ("0", "ENDBLK"), ("5", doc.handle()), ("330", rec_h), ("100", "AcDbEntity"),
                ("8", lname), ("100", "AcDbBlockEnd")]))
            new_blocks += body

            ins_h = doc.handle()
            insert = [_Entity([
                ("0", "INSERT"), ("5", ins_h)] + ([("330", model_space)] if model_space else []) + [
                ("100", "AcDbEntity"), ("8", lname), ("100", "AcDbBlockReference"),
                ("66", "     1" if tags else "     0"), ("2", bname),
                ("10", x), ("20", y), ("30", "0.0")])]
            if tags:
                for i, (tag, value) in enumerate(zip(tags, fb.values)):
                    ty = _fmt(fb.anchor[1] - i * text_height * 1.5)
                    insert.append(_Entity([
                        ("0", "ATTRIB"), ("5", doc.handle()), ("330", ins_h), ("100", "AcDbEntity"),
                        ("8", lname), ("100", "AcDbText"), ("10", x), ("20", ty), ("30", "0.0"),
                        ("40", _fmt(text_height)), ("1", enc(text_value(value, result))),
                        ("100", "AcDbAttribute"), ("2", enc(tag)), ("70", flags)]))
                insert.append(_Entity([
                    ("0", "SEQEND"), ("5", doc.handle()), ("330", ins_h), ("100", "AcDbEntity"),
                    ("8", lname)]))
            out_units[first_pos[fb.key]] = insert
            result.blocks += 1

    tables[bs + 1:be] = tables[bs + 1:be] + new_records
    doc.set_section("TABLES", _join(tables))
    doc.set_section("BLOCKS", _join(blocks) + _join(new_blocks))
    doc.set_section("ENTITIES", [t for u in out_units if u for e in u for t in e.tags])

    header = doc.section("HEADER")
    _header_set(header, "$HANDSEED", "5", "{:X}".format(doc.next_handle))
    if insunits is not None:
        _header_set(header, "$INSUNITS", "70", "{:>6}".format(insunits))

    data = doc.text().encode(codec, errors="replace")
    with open(path, "wb") as f:
        f.write(data)
    return result
