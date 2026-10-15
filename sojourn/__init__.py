def classFactory(iface):
    """Entry point required by QGIS to load the plugin."""
    from .sojourn_plugin import SojournPlugin
    return SojournPlugin(iface)
