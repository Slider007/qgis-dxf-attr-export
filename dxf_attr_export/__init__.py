def classFactory(iface):
    from .plugin import DxfAttrExportPlugin
    return DxfAttrExportPlugin(iface)
