"""Command-line entry point for valleespyr."""

from __future__ import annotations

import json
import sys

import click

from . import watershed as ws
from .dump import dump_layer
from .sources.wfs import GEOPLATEFORME_WFS, LAYER_BASSIN_VERSANT, WFSClient, WFSError

TRONCON_BBOX_HELP = (
    "Bounding box as 'minx,miny,maxx,maxy' (lon/lat WGS84) to fetch tronçons live."
)

BBOX_HELP = "Bounding box as 'minx,miny,maxx,maxy' (lon/lat WGS84 unless --bbox-crs given)."


def _parse_bbox(value: str) -> tuple[float, float, float, float]:
    try:
        parts = tuple(float(x) for x in value.split(","))
    except ValueError as exc:
        raise click.BadParameter("expected 4 comma-separated numbers") from exc
    if len(parts) != 4:
        raise click.BadParameter("expected exactly 4 values: minx,miny,maxx,maxy")
    return parts  # type: ignore[return-value]


def _dump(obj, path: str | None) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        click.echo(f"wrote {path}", err=True)
    else:
        click.echo(text)


@click.group()
@click.version_option(package_name="valleespyr")
@click.option(
    "--wfs-endpoint",
    default=GEOPLATEFORME_WFS,
    show_default=True,
    envvar="VALLEESPYR_WFS_ENDPOINT",
    help="WFS 2.0 base URL (IGN Géoplateforme by default; Sandre also works).",
)
@click.option("--timeout", default=60.0, show_default=True, help="HTTP timeout in seconds.")
@click.pass_context
def cli(ctx: click.Context, wfs_endpoint: str, timeout: float) -> None:
    """valleespyr — explore topographic & watershed data for Pyrénées valleys."""
    ctx.ensure_object(dict)
    ctx.obj["client"] = WFSClient(endpoint=wfs_endpoint, timeout=timeout)


@cli.group()
def wfs() -> None:
    """Explore a WFS source (BD TOPAGE / BD TOPO watersheds)."""


@wfs.command("layers")
@click.option("-k", "--keyword", default=None, help="Filter name/title/abstract (case-insens.).")
@click.pass_context
def wfs_layers(ctx: click.Context, keyword: str | None) -> None:
    """List advertised feature types."""
    client: WFSClient = ctx.obj["client"]
    try:
        fts = client.list_feature_types(keyword=keyword)
    except (WFSError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    if not fts:
        click.echo("(no matching feature types)")
        return
    for ft in fts:
        click.echo(f"{ft.name}")
        if ft.title:
            click.echo(f"    title : {ft.title}")
        if ft.wgs84_bbox:
            click.echo(f"    bbox  : {', '.join(f'{c:.3f}' for c in ft.wgs84_bbox)}")


@wfs.command("schema")
@click.option("--layer", default=LAYER_BASSIN_VERSANT, show_default=True)
@click.pass_context
def wfs_schema(ctx: click.Context, layer: str) -> None:
    """Show a layer's attribute schema (DescribeFeatureType)."""
    client: WFSClient = ctx.obj["client"]
    try:
        fields = client.describe_feature_type(layer)
    except (WFSError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    for name, ftype in fields:
        click.echo(f"{name:<30} {ftype}")


@wfs.command("count")
@click.option("--layer", default=LAYER_BASSIN_VERSANT, show_default=True)
@click.option("--bbox", "bbox_s", default=None, help=BBOX_HELP)
@click.option("--bbox-crs", default="EPSG:4326", show_default=True)
@click.option("--cql", default=None, help="CQL filter (mutually exclusive with --bbox).")
@click.pass_context
def wfs_count(
    ctx: click.Context, layer: str, bbox_s: str | None, bbox_crs: str, cql: str | None
) -> None:
    """Count matching features without downloading them."""
    client: WFSClient = ctx.obj["client"]
    bbox = _parse_bbox(bbox_s) if bbox_s else None
    try:
        n = client.count_features(layer, bbox=bbox, bbox_crs=bbox_crs, cql_filter=cql)
    except (WFSError, OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(n)


@wfs.command("watersheds")
@click.option("--layer", default=LAYER_BASSIN_VERSANT, show_default=True)
@click.option("--bbox", "bbox_s", default=None, help=BBOX_HELP)
@click.option("--bbox-crs", default="EPSG:4326", show_default=True)
@click.option("--point", default=None, help="'lon,lat' WGS84: fetch watershed(s) containing it.")
@click.option("--srs", default="EPSG:4326", show_default=True, help="Output CRS (e.g. EPSG:2154).")
@click.option("--max-features", type=int, default=None, help="Cap on features returned.")
@click.option("-o", "--output", default=None, help="Write GeoJSON here instead of stdout.")
@click.pass_context
def wfs_watersheds(
    ctx: click.Context,
    layer: str,
    bbox_s: str | None,
    bbox_crs: str,
    point: str | None,
    srs: str,
    max_features: int | None,
    output: str | None,
) -> None:
    """Fetch watershed polygons by bounding box or by a contained point, as GeoJSON."""
    client: WFSClient = ctx.obj["client"]
    if (bbox_s is None) == (point is None):
        raise click.UsageError("provide exactly one of --bbox or --point")
    try:
        if point is not None:
            lon, lat = (float(x) for x in point.split(","))
            fc = ws.fetch_watershed_by_point(client, lon, lat, layer=layer, srs_name=srs)
        else:
            fc = ws.fetch_watersheds_bbox(
                client,
                _parse_bbox(bbox_s),  # type: ignore[arg-type]
                layer=layer,
                bbox_crs=bbox_crs,
                srs_name=srs,
                max_features=max_features,
            )
    except (WFSError, OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    n = len(fc.get("features", []))
    click.echo(f"{n} feature(s)", err=True)
    _dump(fc, output)


@wfs.command("dump")
@click.option("--layer", default=LAYER_BASSIN_VERSANT, show_default=True)
@click.option("--bbox", "bbox_s", default=None, help=BBOX_HELP)
@click.option("--bbox-crs", default="EPSG:4326", show_default=True)
@click.option("--cql", default=None, help="CQL filter (mutually exclusive with --bbox).")
@click.option("--srs", default="EPSG:4326", show_default=True, help="Output CRS (e.g. EPSG:2154).")
@click.option("--page-size", default=1000, show_default=True, help="Features per WFS request.")
@click.option("--max-features", type=int, default=None, help="Stop after this many features.")
@click.option(
    "-o",
    "--output",
    required=True,
    help="Destination file. Suffix picks the format: .parquet/.gpkg (needs geopandas) or .geojson.",
)
@click.pass_context
def wfs_dump(
    ctx: click.Context,
    layer: str,
    bbox_s: str | None,
    bbox_crs: str,
    cql: str | None,
    srs: str,
    page_size: int,
    max_features: int | None,
    output: str,
) -> None:
    """Bulk-download a whole layer (paged) to GeoJSON / GeoParquet / GeoPackage.

    With no --bbox and no --cql, downloads the entire layer. The watershed layer
    is ~6.6k features for all of metropolitan France, so that is quick.
    """
    client: WFSClient = ctx.obj["client"]
    if bbox_s is not None and cql is not None:
        raise click.UsageError("provide at most one of --bbox or --cql")
    bbox = _parse_bbox(bbox_s) if bbox_s else None

    def _progress(n: int, total: int | None) -> None:
        click.echo(f"  {n}{f' / {total}' if total else ''} features", err=True)

    try:
        count = dump_layer(
            client,
            layer,
            output,
            bbox=bbox,
            bbox_crs=bbox_crs,
            cql_filter=cql,
            srs_name=srs,
            page_size=page_size,
            max_features=max_features,
            progress=_progress,
        )
    except (WFSError, OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"wrote {count} features to {output}", err=True)


@cli.group()
def hydro() -> None:
    """Stream-network topology (BD TOPO tronçon_hydrographique)."""


@hydro.command("tree")
@click.option(
    "--from-file",
    "from_file",
    default=None,
    help="Local tronçon dump (GeoJSON/GeoParquet). Offline; skips the WFS.",
)
@click.option("--bbox", "bbox_s", default=None, help=TRONCON_BBOX_HELP)
@click.option(
    "--point",
    "point_s",
    default=None,
    help="Pour point 'lon,lat' WGS84. Defaults to the bbox / file centroid.",
)
@click.option(
    "--min-order", type=int, default=None, help="Prune branches below this Strahler order."
)
@click.option("--no-fictif", is_flag=True, help="Hide fictitious edges (lake/void connectors).")
@click.option("--no-collapse", is_flag=True, help="Do not fold unbranched same-river chains.")
@click.option("--max-depth", type=int, default=None, help="Limit printed tree depth.")
@click.option(
    "--json", "as_json", is_flag=True, help="Emit the nested dict as JSON, not a tree."
)
@click.pass_context
def hydro_tree(
    ctx: click.Context,
    from_file: str | None,
    bbox_s: str | None,
    point_s: str | None,
    min_order: int | None,
    no_fictif: bool,
    no_collapse: bool,
    max_depth: int | None,
    as_json: bool,
) -> None:
    """Trace the stream network upstream of a pour point and print it as a tree."""
    from .hydro import (
        build_graph,
        drop_fictif,
        snap_pour_point,
        to_tree,
        trace_upstream,
    )
    from .hydro.network import fetch_troncons, load_troncons

    if (from_file is None) == (bbox_s is None):
        raise click.UsageError("provide exactly one of --from-file or --bbox")

    try:
        if from_file is not None:
            gdf = load_troncons(from_file)
        else:
            client: WFSClient = ctx.obj["client"]
            gdf = fetch_troncons(client, _parse_bbox(bbox_s))  # type: ignore[arg-type]
    except (WFSError, OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    if not len(gdf):
        raise click.ClickException("no tronçons loaded")

    if point_s is not None:
        lon, lat = (float(x) for x in point_s.split(","))
    else:
        minx, miny, maxx, maxy = gdf.total_bounds
        lon, lat = (minx + maxx) / 2, (miny + maxy) / 2
        click.echo(f"no --point; using centroid {lon:.5f},{lat:.5f}", err=True)

    snap = snap_pour_point(gdf, lon, lat)
    click.echo(
        f"snapped to {snap['cleabs']} "
        f"({snap['toponyme'] or 'unnamed'}, {snap['distance_m']:.0f} m away)",
        err=True,
    )

    # Fictif edges (lake/void connectors) stay in the graph for connectivity — they
    # often carry the main stem through a lake — and are folded out of the view below.
    graph = build_graph(gdf)
    edge_ids, sub = trace_upstream(graph, snap["root_node"])
    tree = to_tree(
        sub, snap["root_node"], collapse_chains=not no_collapse, min_order=min_order
    )
    if no_fictif:
        drop_fictif(tree)

    if as_json:
        _dump(tree, None)
        return

    shown = "shown" if (min_order or no_fictif) else "total"
    click.echo(
        f"upstream of {snap['toponyme'] or snap['cleabs']}: "
        f"{len(edge_ids)} edges traced; "
        f"{tree['upstream_segments']} segments / {tree['upstream_length_m'] / 1000:.1f} km {shown}"
        + (" (fictif hidden)" if no_fictif else "")
    )
    for line in _render_tree(tree, max_depth=max_depth):
        click.echo(line)


def _render_tree(node: dict, *, max_depth: int | None) -> list[str]:
    """ASCII tree lines: '├── Gave de Pau  (order 4, 8.2 km, 12 seg)'."""
    lines: list[str] = []

    def label(n: dict) -> str:
        e = n.get("edge")
        if e is None:
            return "● (pour point)"
        name = e.get("toponyme") or "(unnamed)"
        bits = []
        if e.get("order") is not None:
            bits.append(f"order {e['order']}")
        length = e.get("length_m", 0.0) + n.get("collapsed_length_m", 0.0)
        bits.append(f"{length / 1000:.1f} km")
        seg = 1 + n.get("collapsed_segments", 0)
        if seg > 1:
            bits.append(f"{seg} seg")
        if e.get("fictif"):
            bits.append("fictif")
        return f"{name}  ({', '.join(bits)})"

    def walk(n: dict, prefix: str, is_last: bool, depth: int) -> None:
        if depth == 0:
            lines.append(label(n))
        else:
            lines.append(f"{prefix}{'└── ' if is_last else '├── '}{label(n)}")
        if max_depth is not None and depth >= max_depth:
            if n["children"]:
                more = sum(c["upstream_segments"] for c in n["children"])
                child_prefix = prefix + ("    " if is_last else "│   ")
                lines.append(f"{child_prefix}… {len(n['children'])} branch(es), {more} seg")
            return
        kids = n["children"]
        for i, child in enumerate(kids):
            last = i == len(kids) - 1
            child_prefix = prefix + ("" if depth == 0 else ("    " if is_last else "│   "))
            walk(child, child_prefix, last, depth + 1)

    walk(node, "", True, 0)
    return lines


def main() -> None:
    try:
        cli(obj={})
    except click.ClickException as exc:
        exc.show()
        sys.exit(exc.exit_code)


if __name__ == "__main__":
    main()
