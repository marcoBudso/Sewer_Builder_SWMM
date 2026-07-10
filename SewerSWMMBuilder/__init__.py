def classFactory(iface):
    from .plugin import SewerSWMMBuilderPlugin
    return SewerSWMMBuilderPlugin(iface)
