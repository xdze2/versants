"""Stream-network topology: build a directed graph from ``troncon_hydrographique``
edges and trace the network upstream from a pour point."""

from .network import (
    LAYER_TRONCON,
    build_graph,
    fetch_troncons,
    load_troncons,
)
from .trace import drop_fictif, snap_pour_point, to_tree, trace_upstream

__all__ = [
    "LAYER_TRONCON",
    "build_graph",
    "drop_fictif",
    "fetch_troncons",
    "load_troncons",
    "snap_pour_point",
    "to_tree",
    "trace_upstream",
]
