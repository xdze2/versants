"""Command-line entry point for valleespyr."""

from __future__ import annotations

import json
import sys

import click
import requests

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


def _load_troncons_gdf(ctx: click.Context, from_file: str | None, bbox_s: str | None):
    """Shared loader for the hydro commands: local file or a live WFS bbox."""
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
    return gdf


@hydro.group("rivers")
def hydro_rivers() -> None:
    """Roll the tronçon network up into a browsable river graph."""


@hydro_rivers.command("list")
@click.option("--from-file", "from_file", default=None, help="Local tronçon dump (offline).")
@click.option("--bbox", "bbox_s", default=None, help=TRONCON_BBOX_HELP)
@click.option("--named-only", is_flag=True, help="Only rivers that carry a toponyme.")
@click.option("--roots", "roots_only", is_flag=True, help="Only rivers with no river downstream.")
@click.option("--min-length-km", type=float, default=0.0, help="Hide rivers shorter than this.")
@click.option("-n", "--limit", type=int, default=40, show_default=True, help="Max rows (0 = all).")
@click.option("--json", "as_json", is_flag=True, help="Emit the summary rows as JSON.")
@click.pass_context
def rivers_list(
    ctx: click.Context,
    from_file: str | None,
    bbox_s: str | None,
    named_only: bool,
    roots_only: bool,
    min_length_km: float,
    limit: int,
    as_json: bool,
) -> None:
    """List the rivers found in the loaded network, longest first."""
    from .hydro import build_graph, build_river_network

    gdf = _load_troncons_gdf(ctx, from_file, bbox_s)
    rn = build_river_network(build_graph(gdf))

    rows = rn.summary()
    if named_only:
        rows = [r for r in rows if r["name"]]
    if roots_only:
        rows = [r for r in rows if r["is_root"]]
    if min_length_km:
        rows = [r for r in rows if r["length_km"] >= min_length_km]
    if limit:
        rows = rows[:limit]

    if as_json:
        _dump(rows, None)
        return

    click.echo(
        f"{len(rn)} rivers "
        f"({sum(1 for x in rn if x.name)} named, {len(rn.roots())} roots, "
        f"{len(rn.leaves())} leaves); {rn.graph.number_of_edges()} 'flows into' links",
        err=True,
    )
    click.echo(f"{'length':>9}  {'ord':>3}  {'seg':>4}  {'R/L':>3}  {'trib':>4}  name / id")
    for r in rows:
        flag = ("R" if r["is_root"] else " ") + ("L" if r["is_leaf"] else " ")
        click.echo(
            f"{r['length_km']:>7.1f}km  {str(r['max_order'] or ''):>3}  "
            f"{r['n_segments']:>4}  {flag:>3}  {r['n_children']:>4}  {r['name'] or r['id']}"
        )


@hydro_rivers.command("show")
@click.argument("query")
@click.option("--from-file", "from_file", default=None, help="Local tronçon dump (offline).")
@click.option("--bbox", "bbox_s", default=None, help=TRONCON_BBOX_HELP)
@click.option(
    "--geojson",
    type=click.Choice(["path", "catchment"]),
    default=None,
    help="Emit GeoJSON instead of the report: the river's own lines, or its whole catchment.",
)
@click.option("-o", "--output", default=None, help="Write output here instead of stdout.")
@click.pass_context
def rivers_show(
    ctx: click.Context,
    query: str,
    from_file: str | None,
    bbox_s: str | None,
    geojson: str | None,
    output: str | None,
) -> None:
    """Inspect one river: parent, tributaries, catchment rivers, root/leaf state.

    QUERY is a river name (case-insensitive substring) or a ``COURDEAU…`` id.
    """
    from .hydro import build_graph, build_river_network

    gdf = _load_troncons_gdf(ctx, from_file, bbox_s)
    rn = build_river_network(build_graph(gdf))

    river = rn.get(query)
    if river is None:
        matches = rn.by_name(query)
        if not matches:
            raise click.ClickException(f"no river matching {query!r}")
        if len(matches) > 1:
            click.echo(f"{len(matches)} rivers match {query!r}:", err=True)
            for m in sorted(matches, key=lambda r: r.length_m, reverse=True):
                click.echo(f"  {m.length_m / 1000:6.1f} km  {m.name}  [{m.id}]", err=True)
            raise click.ClickException("be more specific or pass the id")
        river = matches[0]

    if geojson is not None:
        fc = (
            rn.river_path_geojson(river.id)
            if geojson == "path"
            else rn.river_catchment_geojson(river.id)
        )
        click.echo(f"{len(fc['features'])} line feature(s)", err=True)
        _dump(fc, output)
        return

    parent = rn.parent(river.id)
    kids = rn.children(river.id)
    catchment = rn.upstream_rivers(river.id, named_only=True)
    state = "root" if river.is_root else "leaf" if river.is_leaf else "interior"
    downstream = (parent.name or parent.id) if parent else "— (drains out of the loaded network)"

    lines = [
        f"{river.name or '(unnamed)'}   [{river.id}]",
        f"  state        : {state}  (root={river.is_root}, leaf={river.is_leaf})",
        f"  length       : {river.length_m / 1000:.1f} km over {len(river.segments)} tronçons",
        f"  Strahler     : {river.max_order}",
        f"  outlet node  : {river.outlet}",
        f"  downstream   : {downstream}",
        f"  direct tribs : {len(kids)}",
    ]
    for c in kids[:20]:
        lines.append(f"      {c.length_m / 1000:6.1f} km  {c.name or c.id}")
    if len(kids) > 20:
        lines.append(f"      … {len(kids) - 20} more")
    lines.append(f"  catchment rivers (named, recursive): {len(catchment)}")
    for c in catchment[:25]:
        lines.append(f"      {c.length_m / 1000:6.1f} km  {c.name}")
    if len(catchment) > 25:
        lines.append(f"      … {len(catchment) - 25} more")
    click.echo("\n".join(lines))


def _load_river_network(ctx: click.Context, from_file: str | None, bbox_s: str | None):
    """Load a tronçon dump / bbox and roll it up into a :class:`RiverNetwork`."""
    from .hydro import build_graph, build_river_network

    gdf = _load_troncons_gdf(ctx, from_file, bbox_s)
    return build_river_network(build_graph(gdf))


def _echo_diag(diag: dict) -> None:
    """Print delineation diagnostics to stderr as a compact aligned block."""
    rows = [
        ("area_km2", f"{diag['area_km2']:.2f}"),
        ("snap_moved_cells", str(diag["snap_moved_cells"])),
        ("depression_fill_frac", f"{diag['depression_fill_frac'] * 100:.2f}%"),
        ("course_inside_frac", f"{diag['course_inside_frac'] * 100:.2f}%"),
        ("n_cells", str(diag["n_cells"])),
        ("dem_tif", str(diag["dem_tif"])),
    ]
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        click.echo(f"  {key:<{width}}  {value}", err=True)


@cli.group()
def valley() -> None:
    """Pick a valley and build its 3D catchment render."""


@valley.command("list")
@click.option("--from-file", "from_file", default=None, help="Local tronçon dump (offline).")
@click.option("--bbox", "bbox_s", default=None, help=TRONCON_BBOX_HELP)
@click.option("--named-only", is_flag=True, help="Only rivers that carry a toponyme.")
@click.option("--min-length-km", type=float, default=0.0, help="Hide rivers shorter than this.")
@click.option("-n", "--limit", type=int, default=40, show_default=True, help="Max rows (0 = all).")
@click.option("--json", "as_json", is_flag=True, help="Emit the rows as JSON.")
@click.pass_context
def valley_list(
    ctx: click.Context,
    from_file: str | None,
    bbox_s: str | None,
    named_only: bool,
    min_length_km: float,
    limit: int,
    as_json: bool,
) -> None:
    """List the loaded rivers as render candidates, longest first.

    A trimmed view aimed at picking a valley to render; ``hydro rivers list``
    has the fuller column set (segments, tributaries, leaf state).
    """
    rn = _load_river_network(ctx, from_file, bbox_s)

    rows = rn.summary()
    if named_only:
        rows = [r for r in rows if r["name"]]
    if min_length_km:
        rows = [r for r in rows if r["length_km"] >= min_length_km]
    if limit:
        rows = rows[:limit]

    if as_json:
        _dump(rows, None)
        return

    click.echo(
        f"{len(rn)} rivers "
        f"({sum(1 for x in rn if x.name)} named, {len(rn.roots())} roots); "
        f"pick one for 'valley catchment' / 'valley render'",
        err=True,
    )
    click.echo(f"{'length':>9}  {'ord':>3}  {'root':>4}  name / id")
    for r in rows:
        click.echo(
            f"{r['length_km']:>7.1f}km  {str(r['max_order'] or ''):>3}  "
            f"{('yes' if r['is_root'] else ''):>4}  {r['name'] or r['id']}"
        )


@valley.command("catchment")
@click.argument("query")
@click.option("--from-file", "from_file", default=None, help="Local tronçon dump (offline).")
@click.option("--bbox", "bbox_s", default=None, help=TRONCON_BBOX_HELP)
@click.option("--demtype", default="COP30", show_default=True, help="OpenTopography DEM type.")
@click.option(
    "--acc-channel-cells",
    type=int,
    default=1000,
    show_default=True,
    help="Flow-accumulation threshold (cells) that defines a channel for the snap.",
)
@click.option(
    "--dem-dir",
    type=click.Path(file_okay=False),
    default=None,
    help="Cache the DEM tile here (default: fetch_dem's own cache location).",
)
@click.option("-o", "--output", default=None, help="Write GeoJSON here instead of stdout.")
@click.pass_context
def valley_catchment(
    ctx: click.Context,
    query: str,
    from_file: str | None,
    bbox_s: str | None,
    demtype: str,
    acc_channel_cells: int,
    dem_dir: str | None,
    output: str | None,
) -> None:
    """Delineate one river's DEM catchment polygon and emit it as GeoJSON.

    QUERY is a river name (case-insensitive substring) or a ``COURDEAU…`` id.
    Needs an OpenTopography API key (``OPENTOPOGRAPHY_API_KEY`` or
    ``key.secret``); the DEM tile is fetched live.
    """
    from pathlib import Path

    from shapely.geometry import mapping

    from . import valley as valley_mod

    rn = _load_river_network(ctx, from_file, bbox_s)
    try:
        river = valley_mod.resolve_river(rn, query)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        poly, diag = valley_mod.delineate_river(
            rn,
            river.id,
            dem_dir=Path(dem_dir) if dem_dir else None,
            demtype=demtype,
            acc_channel_cells=acc_channel_cells,
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_diag(diag)

    feature = {
        "type": "Feature",
        "geometry": mapping(poly),
        "properties": {
            "name": river.name,
            "id": river.id,
            "area_km2": diag["area_km2"],
            "source": "dem_" + demtype.lower(),
        },
    }
    _dump({"type": "FeatureCollection", "features": [feature]}, output)


@valley.group("catchments")
def valley_catchments() -> None:
    """Batch operations over many rivers' catchments (see 'precompute')."""


@valley_catchments.command("precompute")
@click.argument("root_query")
@click.option("--from-file", "from_file", default=None, help="Local tronçon dump (offline).")
@click.option("--bbox", "bbox_s", default=None, help=TRONCON_BBOX_HELP)
@click.option(
    "--bassins",
    "bassins_path",
    type=click.Path(dir_okay=False, exists=True),
    default=None,
    help="Local bassin_versant_topographique dump — any river it already covers "
    "is skipped (WFS wins, DEM only fills gaps).",
)
@click.option(
    "--dem-dir",
    type=click.Path(file_okay=False),
    default="data/raw/dem",
    show_default=True,
    help="Cache DEM tiles here.",
)
@click.option(
    "--dem-source",
    type=click.Choice(["s3", "opentopography"]),
    default="s3",
    show_default=True,
    help="'s3': public, unauthenticated Copernicus GLO-30 tiles, no rate limit "
    "(cached per 1x1 degree cell, shared across nearby rivers). "
    "'opentopography': the REST API, needs a key and caps at 50 downloads/24h.",
)
@click.option("--demtype", default="COP30", show_default=True, help="OpenTopography DEM type.")
@click.option(
    "--acc-channel-cells",
    type=int,
    default=1000,
    show_default=True,
    help="Flow-accumulation threshold (cells) that defines a channel for the snap.",
)
@click.option(
    "--area-min",
    "area_min",
    type=float,
    default=None,
    help="Discard a delineated catchment smaller than this (km²). "
    "Default: catalog._VALLEY_AREA_MIN_KM2.",
)
@click.option(
    "--area-max",
    "area_max",
    type=float,
    default=None,
    help="Discard a delineated catchment bigger than this (km²). "
    "Default: catalog._VALLEY_AREA_MAX_KM2.",
)
@click.option(
    "-o",
    "--out",
    "out_path",
    type=click.Path(dir_okay=False),
    default="data/processed/catchments.json",
    show_default=True,
    help="Output JSON (merged incrementally with whatever is already there).",
)
@click.pass_context
def valley_catchments_precompute(
    ctx: click.Context,
    root_query: str,
    from_file: str | None,
    bbox_s: str | None,
    bassins_path: str | None,
    dem_dir: str,
    dem_source: str,
    demtype: str,
    acc_channel_cells: int,
    area_min: float | None,
    area_max: float | None,
    out_path: str,
) -> None:
    """DEM-delineate a catchment for every river upstream of ROOT_QUERY, as a batch.

    ``bassin_versant_topographique`` (the WFS watershed dump) only covers a small
    fraction of named rivers, so the HTML catalog's per-valley map mask (see
    ``valleespyr catalog --geo --bassins``) is blank for most of them. This
    command fills that gap offline, ahead of time: for every river in
    ROOT_QUERY's upstream network that isn't already covered by ``--bassins``
    (when given) or already present in ``--out``, it delineates a catchment from
    a Copernicus DEM (:func:`valleespyr.valley.delineate_river`) and keeps the
    result only if the *measured* area falls in ``[--area-min, --area-max]`` —
    the same "reads as one valley" range ``valleespyr catalog`` uses. Load the
    output's ``"rivers"`` dict and pass it as ``build_catalog(..., dem_catchments=...)``
    to use it.

    Needs the ``dem`` extra installed. With the default ``--dem-source s3``
    (public, unauthenticated Copernicus tiles) no API key or quota applies.
    With ``--dem-source opentopography`` an API key is needed
    (``OPENTOPOGRAPHY_API_KEY`` or ``key.secret``) and its free tier caps at 50
    downloads/24h; hitting that quota stops the batch early (after writing out
    everything collected so far) rather than crashing.

    Safe to re-run either way: already-cached rivers (successes only) are
    skipped, so an interrupted run can be resumed, and a river that failed or
    fell outside the area range is retried next time.

    ROOT_QUERY is a river name (case-insensitive substring) or a ``COURDEAU…``
    id, resolved the same way as the other ``valley`` commands.
    """
    from pathlib import Path

    from . import valley as valley_mod
    from .catalog import _VALLEY_AREA_MAX_KM2, _VALLEY_AREA_MIN_KM2, polygon_to_rings

    area_min = _VALLEY_AREA_MIN_KM2 if area_min is None else area_min
    area_max = _VALLEY_AREA_MAX_KM2 if area_max is None else area_max

    rn = _load_river_network(ctx, from_file, bbox_s)
    try:
        root = valley_mod.resolve_river(rn, root_query)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    candidates = [root] + rn.upstream_rivers(root.id)

    bassins = None
    if bassins_path:
        bassins = ws.load_bassins(bassins_path)

    out_file = Path(out_path)
    if out_file.exists():
        existing = json.loads(out_file.read_text("utf-8"))
        rivers_out: dict = existing.get("rivers", {})
    else:
        rivers_out = {}

    n_skipped_wfs = 0
    n_skipped_cached = 0
    n_kept = 0
    n_discarded = 0
    n_failed = 0
    stopped_early = None

    def _write_out() -> None:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "meta": {
                "demtype": demtype,
                "acc_channel_cells": acc_channel_cells,
                "area_min_km2": area_min,
                "area_max_km2": area_max,
                "root_id": root.id,
            },
            "rivers": rivers_out,
        }
        out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    for river in candidates:
        if river.id in rivers_out:
            n_skipped_cached += 1
            continue
        if bassins is not None:
            ids = {river.id} | {r.id for r in rn.upstream_rivers(river.id)}
            if ws.catchment_polygon(bassins, ids) is not None:
                n_skipped_wfs += 1
                continue

        try:
            poly, diag = valley_mod.delineate_river(
                rn,
                river.id,
                dem_dir=Path(dem_dir),
                demtype=demtype,
                acc_channel_cells=acc_channel_cells,
                dem_source=dem_source,
            )
        except requests.exceptions.HTTPError as exc:
            # OpenTopography's daily-quota rejection comes back as a 401 (not
            # a 429) with a "rate limit" XML body — not "bad key", "no more
            # calls today". fetch_dem() folds that body into the exception's
            # own message (see hydro/dem.py) since by the time it reaches us
            # here the underlying streamed response is already closed and
            # exc.response.text would just be empty. This is the one failure
            # that isn't specific to this river — stop the whole batch (after
            # writing out what we have) instead of ploughing into the same
            # wall for every river left.
            if "rate limit" in str(exc).lower():
                stopped_early = (
                    "hit OpenTopography's daily rate limit "
                    f"({river.name or river.id} was next) — re-run this "
                    "command later today or tomorrow to pick up where it left off"
                )
                break
            click.echo(f"warning: {river.name or river.id}: {exc}", err=True)
            n_failed += 1
            continue
        except Exception as exc:  # noqa: BLE001 - a batch over ~600 rivers of
            # real terrain will hit edge cases pysheds doesn't raise a
            # RuntimeError for (e.g. an empty channel mask -> IndexError deep
            # inside grid.snap_to_mask when a tile has no cell above
            # acc_channel_cells). One river's oddity shouldn't sink the run.
            click.echo(f"warning: {river.name or river.id}: {exc}", err=True)
            n_failed += 1
            continue

        area = diag["area_km2"]
        if not (area_min <= area <= area_max):
            click.echo(
                f"discarding {river.name or river.id}: {area:.1f} km² outside "
                f"[{area_min}, {area_max}]",
                err=True,
            )
            n_discarded += 1
            continue

        rings = polygon_to_rings(poly)
        if rings is None:
            click.echo(f"warning: {river.name or river.id}: empty polygon after simplify", err=True)
            n_failed += 1
            continue

        rivers_out[river.id] = {
            "catchment": rings,
            "area_km2": round(area, 1),
            "source": "dem",
            "outlet_lonlat": list(diag["outlet_lonlat"]),
            "snap_moved_cells": diag["snap_moved_cells"],
        }
        n_kept += 1
        click.echo(f"kept {river.name or river.id}: {area:.1f} km²", err=True)
        # write after every kept river, not just at the end: a batch this size
        # runs long enough to hit an unrecoverable native crash (GDAL/rasterio
        # memory corruption, a killed process, ...) that no Python except can
        # catch — saving incrementally means a crash loses at most the one
        # river in flight, not the whole run since the last save.
        _write_out()

    _write_out()

    click.echo(
        f"{len(candidates)} river(s) considered under {root.name or root.id}: "
        f"{n_kept} kept, {n_skipped_wfs} skipped (WFS-covered), "
        f"{n_skipped_cached} skipped (already cached), "
        f"{n_discarded} discarded (out of area range), {n_failed} failed",
        err=True,
    )
    click.echo(f"wrote {out_file} ({len(rivers_out)} total cached river(s))", err=True)
    if stopped_early:
        click.echo(f"stopped early: {stopped_early}", err=True)


@valley.command("render")
@click.argument("query")
@click.option("--from-file", "from_file", default=None, help="Local tronçon dump (offline).")
@click.option("--bbox", "bbox_s", default=None, help=TRONCON_BBOX_HELP)
@click.option("--demtype", default="COP30", show_default=True, help="OpenTopography DEM type.")
@click.option(
    "--acc-channel-cells",
    type=int,
    default=1000,
    show_default=True,
    help="Flow-accumulation threshold (cells) that defines a channel for the snap.",
)
@click.option(
    "--dem-dir",
    type=click.Path(file_okay=False),
    default=None,
    help="Cache the DEM tile here (default: fetch_dem's own cache location).",
)
@click.option(
    "--exaggeration",
    type=float,
    default=1.0,
    show_default=True,
    help="Vertical exaggeration of the diorama (1.0 = true scale).",
)
@click.option(
    "-o", "--output", required=True, help="Destination .html file (self-contained, no server)."
)
@click.pass_context
def valley_render(
    ctx: click.Context,
    query: str,
    from_file: str | None,
    bbox_s: str | None,
    demtype: str,
    acc_channel_cells: int,
    dem_dir: str | None,
    exaggeration: float,
    output: str,
) -> None:
    """Delineate one river's catchment and bake it into a 3D diorama HTML file.

    QUERY is a river name (case-insensitive substring) or a ``COURDEAU…`` id.
    Needs an OpenTopography API key (``OPENTOPOGRAPHY_API_KEY`` or
    ``key.secret``). The output HTML is self-contained (three.js from a CDN at
    view time; everything else baked in) — no server.
    """
    from pathlib import Path

    from . import valley as valley_mod
    from .render.diorama import build_diorama

    rn = _load_river_network(ctx, from_file, bbox_s)
    try:
        river = valley_mod.resolve_river(rn, query)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        poly, diag = valley_mod.delineate_river(
            rn,
            river.id,
            dem_dir=Path(dem_dir) if dem_dir else None,
            demtype=demtype,
            acc_channel_cells=acc_channel_cells,
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_diag(diag)
    if diag["course_inside_frac"] < 0.9:
        click.echo(
            f"warning: only {diag['course_inside_frac'] * 100:.0f}% of the river's course "
            "falls inside the delineated catchment — the snap may be off",
            err=True,
        )

    streams_fc = rn.river_catchment_geojson(river.id)
    build_diorama(
        poly,
        Path(diag["dem_tif"]),
        streams_fc,
        Path(output),
        title=river.name or river.id,
        z_exaggeration=exaggeration,
    )
    click.echo(f"wrote {output}", err=True)
    click.echo(str(Path(output).resolve()))


@valley.command("plate")
@click.argument("query")
@click.option("--from-file", "from_file", default=None, help="Local tronçon dump (offline).")
@click.option("--bbox", "bbox_s", default=None, help=TRONCON_BBOX_HELP)
@click.option("--demtype", default="COP30", show_default=True, help="OpenTopography DEM type.")
@click.option(
    "--acc-channel-cells",
    type=int,
    default=1000,
    show_default=True,
    help="Flow-accumulation threshold (cells) that defines a channel for the snap.",
)
@click.option(
    "--dem-dir",
    type=click.Path(file_okay=False),
    default=None,
    help="Cache the DEM tile here (default: fetch_dem's own cache location).",
)
@click.option(
    "--crs",
    default="EPSG:2154",
    show_default=True,
    help="Projected CRS to draw in (Lambert-93 for the French Pyrénées).",
)
@click.option("--no-png", is_flag=True, help="Write only the SVG, skip the sibling PNG.")
@click.option(
    "--refresh-osm", is_flag=True, help="Re-query Overpass even if the extent is cached."
)
@click.option(
    "--hillshade",
    is_flag=True,
    help="Compute the four relief fields (hillshade, slope, core & cast shadow) "
         "and drape them under the line work. All numpy, no Blender.",
)
@click.option(
    "--style",
    type=click.Choice(["ign", "real"]),
    default="ign",
    show_default=True,
    help="Compositing preset for --hillshade. 'ign': NW 315deg / 45deg raking "
         "light + a zenithal second light, no cast shadow — the map-legible "
         "estompage (light from the top of the sheet, so the brain reads relief "
         "right). 'real': lower sun, cast-shadow overlay on — physically honest "
         "occlusion.",
)
@click.option(
    "--sun-azimuth", type=float, default=None,
    help="Override the preset sun compass bearing (0 = from the north, clockwise).",
)
@click.option(
    "--sun-altitude", type=float, default=None,
    help="Override the preset sun height above the horizon, degrees.",
)
@click.option(
    "--zenith-weight", type=float, default=None,
    help="Override the zenithal second-light blend: (1-w)*hillshade + w*(1-slope). "
         "0 = single sun, ~0.55 = IGN.",
)
@click.option(
    "--core-strength", type=float, default=None,
    help="Override the core-shadow (terminator, n·L<=0) ink alpha. 0 = off; the "
         "hillshade wash already darkens those faces.",
)
@click.option(
    "--cast-strength", type=float, default=None,
    help="Override the cast-shadow (projected, blocked upwind) overlay alpha. "
         "0 = hillshade only.",
)
@click.option(
    "--shade-gain", type=float, default=1.0, show_default=True,
    help="Cartographic slope exaggeration for the hillshade/slope fields only "
         "(1.0 = faithful; never affects the shadow geometry).",
)
@click.option("-o", "--output", required=True, help="Destination .svg file.")
@click.pass_context
def valley_plate(
    ctx: click.Context,
    query: str,
    from_file: str | None,
    bbox_s: str | None,
    demtype: str,
    acc_channel_cells: int,
    dem_dir: str | None,
    crs: str,
    no_png: bool,
    refresh_osm: bool,
    hillshade: bool,
    style: str,
    sun_azimuth: float | None,
    sun_altitude: float | None,
    zenith_weight: float | None,
    core_strength: float | None,
    cast_strength: float | None,
    shade_gain: float,
    output: str,
) -> None:
    """Render one river's catchment as a minimal black-and-white topo plate.

    Delineates the catchment from a DEM (as ``valley render`` does), pulls the
    human layer — trails, GR/HR routes, roads, refuges and cabanes, named
    summits and cols — from OpenStreetMap, and draws it all as an SVG (+ a
    sibling PNG unless ``--no-png``). Needs an OpenTopography API key
    (``OPENTOPOGRAPHY_API_KEY`` or ``key.secret``); the DEM tile and the
    Overpass response are both cached under ``data/raw``.

    With ``--hillshade`` the four relief fields are computed once (hillshade,
    slope, core shadow, cast shadow — all numpy, no Blender) and ``--style``
    picks how they are composited:

    * ``ign`` (default) — the map convention. NW raking light (315deg) at 45deg
      blended with a zenithal second light (``1 - slope``) so lee slopes stay
      modelled, and *no* cast shadow. The sun sits at the top of the sheet
      (= north on a north-up plate) because the eye reads "light from above"
      and inverts relief lit from below; not physically realistic, and it does
      not need to be.
    * ``real`` — a lower single sun with the cast-shadow overlay on: physically
      honest occlusion (true DEM, true altitude). Better suited to a rotatable
      3-D view than a fixed north-up plate.

    Any of ``--sun-azimuth``, ``--sun-altitude``, ``--zenith-weight``,
    ``--core-strength``, ``--cast-strength`` override the preset.
    """
    from pathlib import Path

    from . import valley as valley_mod
    from .render.plate import build_plate
    from .sources.osm import fetch_osm_features

    # --- relief style preset (individual --flags override) ---------------
    presets = {
        # NW sun high, zenithal fill, estompage only
        "ign": dict(azimuth=315.0, altitude=45.0, zenith=0.55, core=0.0, cast=0.0),
        # low single sun, projected-shadow occlusion shown
        "real": dict(azimuth=315.0, altitude=28.0, zenith=0.0, core=0.0, cast=0.62),
    }
    p = presets[style]
    sun_az = p["azimuth"] if sun_azimuth is None else sun_azimuth
    sun_alt = p["altitude"] if sun_altitude is None else sun_altitude
    zen_w = p["zenith"] if zenith_weight is None else zenith_weight
    core_s = p["core"] if core_strength is None else core_strength
    cast_s = p["cast"] if cast_strength is None else cast_strength

    rn = _load_river_network(ctx, from_file, bbox_s)
    try:
        river = valley_mod.resolve_river(rn, query)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        poly, diag = valley_mod.delineate_river(
            rn,
            river.id,
            dem_dir=Path(dem_dir) if dem_dir else None,
            demtype=demtype,
            acc_channel_cells=acc_channel_cells,
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_diag(diag)
    if diag["course_inside_frac"] < 0.9:
        click.echo(
            f"warning: only {diag['course_inside_frac'] * 100:.0f}% of the river's course "
            "falls inside the delineated catchment — the snap may be off",
            err=True,
        )

    west, south, east, north = poly.bounds
    osm = fetch_osm_features((west, south, east, north), refresh=refresh_osm)

    streams_fc = rn.river_catchment_geojson(river.id)
    subtitle = (
        f"{diag['area_km2']:.1f} km²  ·  contours 20 m (index 100 m)  ·  COP30  ·  OSM"
    )
    written = build_plate(
        poly,
        Path(diag["dem_tif"]),
        streams_fc,
        osm,
        Path(output),
        title=river.name or river.id,
        subtitle=subtitle,
        crs=crs,
        write_png=not no_png,
        hillshade=(sun_az, sun_alt) if hillshade else None,
        shade_gain=shade_gain,
        zenith_weight=zen_w,
        core_strength=core_s,
        cast_strength=cast_s,
    )
    if hillshade:
        click.echo(
            f"relief: style={style} sun az{sun_az:g}/alt{sun_alt:g} "
            f"zenith_weight={zen_w:g} core_strength={core_s:g} "
            f"cast_strength={cast_s:g}",
            err=True,
        )
    for p in written:
        click.echo(f"wrote {p}", err=True)
    click.echo(str(Path(output).resolve()))


@cli.command("catalog")
@click.argument("root_query")
@click.option("--from-file", "from_file", default=None, help="Local tronçon dump (offline).")
@click.option("--bbox", "bbox_s", default=None, help=TRONCON_BBOX_HELP)
@click.option(
    "--bassins",
    "bassins_path",
    type=click.Path(dir_okay=False, exists=True),
    default=None,
    help="Local bassin_versant_topographique dump — adds a shape icon + area per "
    "valley where a sub-basin covers it.",
)
@click.option(
    "--max-depth",
    type=int,
    default=None,
    help="Fold the tree beyond this many confluences from the root into "
    "'+N rivers' leaves (default: no limit). Applies to both the JSON and the "
    "HTML graph.",
)
@click.option(
    "--no-orient-outlet-down",
    is_flag=True,
    help="Keep icon outlines north-up instead of rotating each so its outlet points down.",
)
@click.option(
    "--geo",
    is_flag=True,
    help="Embed a simplified [lon, lat] centreline + outlet per river, so the HTML "
    "gets a click-to-draw mini-map of the selected river's network.",
)
@click.option(
    "-o", "--output", default=None, help="Write the catalog JSON here (default: stdout)."
)
@click.option(
    "--html",
    "html_output",
    default=None,
    help="Also write a self-contained HTML river-git-graph here.",
)
@click.pass_context
def catalog_cmd(
    ctx: click.Context,
    root_query: str,
    from_file: str | None,
    bbox_s: str | None,
    bassins_path: str | None,
    max_depth: int | None,
    no_orient_outlet_down: bool,
    geo: bool,
    output: str | None,
    html_output: str | None,
) -> None:
    """Walk the river graph from ROOT_QUERY into a git-shaped valley-tree JSON.

    ROOT_QUERY is a river name (case-insensitive substring) or a ``COURDEAU…``
    id. Each node splits its upstream into a ``mainline`` (the same valley
    continuing) and ``tributaries`` (branches merging in), carries length,
    Strahler order, valleys upstream and a study-local Pfafstetter code, and —
    with ``--bassins`` — a drainage ``area_km2``. ``--html`` renders it as a
    static git-graph: one lane per river, tinted by Strahler order; add
    ``--geo`` and each row also draws the selected river's network on a map.
    """
    from .catalog import build_catalog
    from .render.catalog_html import render_catalog_html

    rn = _load_river_network(ctx, from_file, bbox_s)

    bassins = None
    if bassins_path:
        from .watershed import load_bassins

        bassins = load_bassins(bassins_path)

    try:
        catalog = build_catalog(
            rn,
            root_query,
            bassins=bassins,
            max_depth=max_depth,
            orient_outlet_down=not no_orient_outlet_down,
            geo=geo,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    m = catalog["meta"]
    click.echo(
        f"{m['n_nodes']} rivers under {m['root_name'] or m['root_id']}"
        + (f"; areas from {bassins_path}" if m["has_icons"] else ""),
        err=True,
    )
    _dump(catalog, output)
    if html_output:
        render_catalog_html(catalog, html_output)
        click.echo(f"wrote {html_output}", err=True)


@valley.command("diorama")
@click.argument("slug")
@click.option(
    "--view",
    default=None,
    help="Named view block in valley.toml (default: the first one).",
)
@click.option(
    "--valleys-dir",
    type=click.Path(file_okay=False),
    default="data/valleys",
    show_default=True,
    help="Directory holding <slug>/valley.toml.",
)
@click.option(
    "--blender",
    default="blender",
    show_default=True,
    envvar="VALLEESPYR_BLENDER",
    help="Blender executable for the headless render.",
)
@click.option(
    "--save-blend/--no-save-blend",
    default=True,
    show_default=True,
    help="Write a .blend next to the PNG for hand-tweaking.",
)
@click.option(
    "--skip-prep",
    is_flag=True,
    help="Reuse the existing derived/ files; only re-run the Blender render.",
)
@click.pass_context
def valley_diorama(
    ctx: click.Context,
    slug: str,
    view: str | None,
    valleys_dir: str,
    blender: str,
    save_blend: bool,
    skip_prep: bool,
) -> None:
    """Render a valley's catchment as a sun-lit isometric diorama PNG (Blender).

    Reads ``<valleys-dir>/<slug>/valley.toml``. Unless ``--skip-prep``, it first
    delineates the catchment, clips the DEM and writes ``derived/terrain.{npy,json}``
    + ``derived/streams.json``; then it shells out to
    ``blender --background --python scripts/render_valley.py``. The PNG lands at
    ``<valleys-dir>/<slug>/<slug>_<view>_3d.png``, with a sibling ``.blend`` for
    hand-tweaking unless ``--no-save-blend``.

    Needs an OpenTopography API key (``OPENTOPOGRAPHY_API_KEY`` or
    ``key.secret``) for the DEM fetch (cached after the first run) and a
    Blender on ``PATH`` (or ``--blender`` / ``VALLEESPYR_BLENDER``).
    """
    import shutil
    import subprocess
    import tomllib
    from pathlib import Path

    valley_dir = Path(valleys_dir) / slug
    toml_path = valley_dir / "valley.toml"
    if not toml_path.exists():
        raise click.ClickException(f"no valley.toml at {toml_path}")
    cfg = tomllib.loads(toml_path.read_text("utf-8"))

    views = cfg.get("view", {})
    if not views:
        raise click.ClickException("valley.toml has no [view.*] block")
    if view is None:
        view = next(iter(views))
        click.echo(f"no --view; using {view!r}", err=True)
    elif view not in views:
        raise click.ClickException(
            f"view {view!r} not in valley.toml (have: {', '.join(views)})"
        )

    if not skip_prep:
        _diorama_prep(ctx, cfg, valley_dir)

    for needed in ("terrain.npy", "terrain.json"):
        if not (valley_dir / "derived" / needed).exists():
            raise click.ClickException(
                f"missing derived/{needed} — run without --skip-prep first"
            )

    blender_exe = shutil.which(blender) or blender
    script = Path(__file__).resolve().parents[2] / "scripts" / "render_valley.py"
    cmd = [
        blender_exe,
        "--background",
        "--python",
        str(script),
        "--",
        str(valley_dir),
        "--view",
        view,
    ]
    if not save_blend:
        cmd.append("--no-save-blend")
    click.echo(f"$ {' '.join(cmd)}", err=True)
    proc = subprocess.run(cmd, check=False)
    if proc.returncode != 0:
        raise click.ClickException(f"blender exited {proc.returncode}")

    out_png = valley_dir / f"{slug}_{view}_3d.png"
    click.echo(f"wrote {out_png}", err=True)
    click.echo(str(out_png.resolve()))


def _diorama_prep(ctx: click.Context, cfg: dict, valley_dir: Path) -> None:
    """Delineate the catchment and write derived/{terrain,streams}.* for Blender."""
    from pathlib import Path

    from shapely.geometry import mapping

    from . import valley as valley_mod
    from .render.terrain_export import export_streams, export_terrain

    river_cfg = cfg.get("river", {})
    catch_cfg = cfg.get("catchment", {})
    terrain_cfg = cfg.get("terrain", {})

    troncons = river_cfg.get("troncons")
    if not troncons:
        raise click.ClickException("valley.toml [river].troncons is required for prep")

    rn = _load_river_network(ctx, str(troncons), None)
    query = river_cfg.get("id") or river_cfg.get("name")
    if not query:
        raise click.ClickException("valley.toml needs [river].id or [river].name")
    try:
        river = valley_mod.resolve_river(rn, query)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    derived = valley_dir / "derived"
    derived.mkdir(parents=True, exist_ok=True)
    try:
        poly, diag = valley_mod.delineate_river(
            rn,
            river.id,
            dem_dir=derived,
            demtype=catch_cfg.get("demtype", "COP30"),
            acc_channel_cells=int(catch_cfg.get("acc_channel_cells", 1000)),
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_diag(diag)
    if diag["course_inside_frac"] < 0.9:
        click.echo(
            f"warning: only {diag['course_inside_frac'] * 100:.0f}% of the river's course "
            "falls inside the delineated catchment — the snap may be off",
            err=True,
        )

    (derived / "catchment.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": mapping(poly),
                        "properties": {"name": river.name, "id": river.id, **{
                            k: diag[k] for k in ("area_km2",) if k in diag
                        }},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    _npy, json_path = export_terrain(
        poly,
        Path(diag["dem_tif"]),
        derived,
        pad_cells=int(terrain_cfg.get("pad_cells", 6)),
    )
    meta = json.loads(json_path.read_text("utf-8"))
    export_streams(rn.river_catchment_geojson(river.id), meta, derived)


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
