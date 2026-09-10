"""Stream-network topology: build a directed graph from ``troncon_hydrographique``
edges, trace it upstream from a pour point, and roll it up into a river graph.

:mod:`.dem` delineates a catchment polygon straight from a DEM, for the rivers
the ``bassin_versant_topographique`` layer cannot cover. It is *not* re-exported
here: it needs the optional ``dem`` extra, so import it as
``from valleespyr.hydro.dem import delineate`` only where that extra is present."""

from .network import (
    LAYER_TRONCON,
    build_graph,
    fetch_troncons,
    load_troncons,
)
from .rivers import River, RiverNetwork, build_river_network
from .trace import drop_fictif, snap_pour_point, to_tree, trace_upstream

__all__ = [
    "LAYER_TRONCON",
    "River",
    "RiverNetwork",
    "build_graph",
    "build_river_network",
    "drop_fictif",
    "fetch_troncons",
    "load_troncons",
    "snap_pour_point",
    "to_tree",
    "trace_upstream",
]
