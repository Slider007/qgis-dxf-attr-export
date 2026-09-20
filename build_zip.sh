#!/bin/sh
# Собирает dist/dxf_attr_export-<версия>.zip для «Модули → Установить из ZIP».
set -e
cd "$(dirname "$0")"
VERSION=$(sed -n 's/^version=//p' dxf_attr_export/metadata.txt)
mkdir -p dist
ZIP="dist/dxf_attr_export-$VERSION.zip"
rm -f "$ZIP"
zip -qr "$ZIP" dxf_attr_export -x '*__pycache__*' '*.pyc' '*.DS_Store'
echo "$ZIP"
