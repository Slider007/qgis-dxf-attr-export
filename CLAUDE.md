# Плагин QGIS «Экспорт в DXF с атрибутами»

Устройство и возможности — в `README.md`. Общие правила модулей компании — в
`~/Projects/QGIS/CLAUDE.md`.

## Как выпускать версию

1. `tests/run_tests.sh` — все проверки должны пройти.
2. Поднять `version=` и дописать `changelog=` в `dxf_attr_export/metadata.txt`.
3. После подтверждения — коммит и пуш, затем релиз с архивом:
   `gh release create vX.Y.Z "$(./build_zip.sh)" --title X.Y.Z --notes "…"`.
4. Дальнейшие шаги — в локальном `CLAUDE.local.md`.

## Устройство

- `processing/export_dxf.py` — алгоритм `dxfattrexport:exportdxf`. В
  `prepareAlgorithm` (основной поток) слои копируются через `materialize` со
  стилем, в копию добавляется поле `__dxf_attr_key` = `QFX_<n>`. Номер поля
  отдаётся `QgsDxfExport.DxfLayer` как поле имени слоя — так у каждого объекта
  в DXF свой временный слой.
- `dxf_blocks.py` — без QGIS: разбор DXF по парам «код — значение», перенос фигур
  объекта в блок (BLOCK_RECORD + BLOCK с ATTDEF), вставка INSERT + ATTRIB + SEQEND
  на месте первой фигуры, TEXT/MTEXT (подписи) остаются в модели. Базовая точка
  блока = точка вставки, поэтому координаты фигур не пересчитываются.
- Режим «только границы» (`BLOCK_CONTENT=1`): фигуры QGIS объекта выбрасываются,
  в блок пишутся LWPOLYLINE/POINT из `FeatureBlock.shapes`, построенных по
  геометрии в `outline_shapes`; цвет — `outline_color` по символу рендерера.
  Объект, скрытый стилем (`willRenderFeature` = False), не выгружается, как и в
  полном режиме.
- `plugin.py` — кнопка открывает `processing.execAlgorithmDialog` с включёнными
  векторными слоями и СК проекта.

## Грабли

- **Кодировка.** QGIS 3 пишет байты в выбранной кодировке (заголовок
  $DWGCODEPAGE ей соответствует), QGIS 4 при том же заголовке пишет UTF-8 —
  читатели DXF показывают такой файл кракозябрами. `dxf_blocks.read_codec`
  распознаёт файл по содержимому, а записывает всегда в объявленной кодировке,
  то есть попутно исправляет файл QGIS 4. В QGIS 4 список `QgsDxfExport.encodings()`
  в нижнем регистре («cp1251»), поиск кодировки — без учёта регистра.
- **Версия DXF.** 3.40 пишет AC1015, 3.44 и 4.2 — AC1018. По версии кодировку
  определять нельзя (см. выше).
- `QgsDxfExport` переносит в DXF только «Простую заливку»: остальные заливки
  дают лишь контур. Штриховку линиями модуль рисует сам (`hatch_lines` —
  линиями, `_hatch_entities` — штриховкой AutoCAD `_USER` с узором в самом файле).

- Папка плагина `dxf_attr_export` — это id у пользователей, не менять.
- `QgsDxfExport` сам пропускает подпись, если в ней есть символ вне кодировки.
- `QgsMapLayer.title()` в 3.40 устарел: заголовок — `serverProperties().title()`.
- Memory-слой в тестах: строковое поле без длины ограничено 255 символами, объект
  с более длинным значением молча не добавляется — задавать `string(1000)`.
- Проверка DXF строгим валидатором: `ezdxf` (`ezdxf.recover.readfile` + `audit`)
  во временном venv вне QGIS; в модуль `ezdxf` не входит.
- Кнопка, пункт меню и панель — общие «Альтан-Эко»: `altan_toolbar.py` и
  `altan_logo.svg` копируются из `~/Projects/QGIS/shared/` без изменений.
- Сборки 3.44+ и 4.x устроены иначе: python в `Contents/MacOS/python3.12`,
  нужен `PYTHONHOME=Contents/Frameworks`, python QGIS в `Contents/Resources/qgis/python`.
  `tests/run_tests.sh` это учитывает; другая версия — `QGIS_APP=…`.
- Проверка в настоящем QGIS: `QGIS --profiles-path <tmp> --code check.py`, плагин
  включается в `<tmp>/profiles/default/qgis.org/QGIS3.ini`. Окно алгоритма модальное:
  закрывать его таймером, найдя через `QApplication.activeModalWidget()`.
