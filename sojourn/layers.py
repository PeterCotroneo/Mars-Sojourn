"""Layer-tree helpers. Sojourn's layers live in a "Mars Rovers" group in the
Layers panel, with one subgroup per mission (its rover, stops, route and the
close-up imagery around it) and the global Mars mosaic at the bottom:

    Mars Rovers
        Perseverance        rover, stops, route
            Ingenuity       final position, landing spots, flights
                            then Perseverance's imagery
        Curiosity           rover, stops, route, imagery
        Mars - global mosaic

Ingenuity sits inside Perseverance's group, above its imagery: it flew over the
same ground, so anywhere below that imagery it would be hidden under it.
"""

from qgis.core import (
    QgsEditorWidgetSetup, QgsLayerTreeGroup, QgsMapLayerLegend, QgsProject,
)

ROOT_GROUP = "Mars Rovers"
_RANK = "sojourn/rank"   # hidden per-node order: lower ranks sit above


class NoLegend(QgsMapLayerLegend):
    """No legend entries: imagery would otherwise list its red, green and blue
    bands in the Layers panel, which means nothing to anyone here."""

    def createLayerTreeModelLegendNodes(self, _node_layer):
        return []


def _insert_ranked(group, rank, add):
    """Insert a child into ``group`` by rank (lower ranks above). ``add(index)``
    creates the node at that index and returns it."""
    index = 0
    for child in group.children():
        other = child.customProperty(_RANK)
        if other is not None and int(other) < rank:
            index += 1
    node = add(index)
    node.setCustomProperty(_RANK, rank)
    return node


def _root_group():
    root = QgsProject.instance().layerTreeRoot()
    for child in root.children():
        if isinstance(child, QgsLayerTreeGroup) and child.name() == ROOT_GROUP:
            return child
    return root.insertGroup(0, ROOT_GROUP)


def _sub_group(path):
    """The group at ``path`` ([(name, rank), ...], outermost first) inside the
    Mars Rovers group, created where missing."""
    parent = _root_group()
    for name, rank in path:
        found = next((c for c in parent.children()
                      if isinstance(c, QgsLayerTreeGroup) and c.name() == name), None)
        if found is None:
            found = _insert_ranked(parent, rank, lambda i, p=parent, n=name: p.insertGroup(i, n))
        parent = found
    return parent


def hide_fields(layer, names):
    """Hide internal fields (the hover text) from Identify results and forms."""
    for name in names:
        idx = layer.fields().indexOf(name)
        if idx >= 0:
            layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup("Hidden", {}))


def layer_valid(layer):
    try:
        return layer is not None and layer.isValid()
    except RuntimeError:   # the user deleted the layer; its C++ object is gone
        return False


def add_layer(layer, rank, group=None):
    """Register ``layer`` and put it in the Mars Rovers group, inside the
    subgroup at path ``group`` ([(name, rank), ...]) if given, ordered by
    ``rank`` (lower ranks above)."""
    QgsProject.instance().addMapLayer(layer, False)
    parent = _sub_group(group) if group else _root_group()
    _insert_ranked(parent, rank, lambda i: parent.insertLayer(i, layer))


def other_layers(ours):
    """Layers in the project that are not in ``ours`` (a set of layer ids)."""
    return [lyr for lid, lyr in QgsProject.instance().mapLayers().items() if lid not in ours]


def remove_layers(layers):
    """Remove layers, then any Sojourn group left empty."""
    project = QgsProject.instance()
    ids = [lyr.id() for lyr in layers if layer_valid(lyr)]
    if ids:
        project.removeMapLayers(ids)
    root = project.layerTreeRoot()
    group = root.findGroup(ROOT_GROUP)
    if group is None:
        return
    _prune(group)
    if not group.children():
        root.removeChildNode(group)


def _prune(group):
    """Remove empty subgroups, innermost first (Ingenuity's group sits inside
    Perseverance's)."""
    for child in list(group.children()):
        if isinstance(child, QgsLayerTreeGroup):
            _prune(child)
            if not child.children():
                group.removeChildNode(child)
