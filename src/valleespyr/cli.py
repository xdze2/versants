"""Command-line entry point for valleespyr."""

from __future__ import annotations

import json
import sys

import click

from . import watershed as ws
from .sources.wfs import GEOPLATEFORME_WFS, LAYER_BASSIN_VERSANT, WFSClient, WFSError

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


def main() -> None:
    try:
        cli(obj={})
    except click.ClickException as exc:
        exc.show()
        sys.exit(exc.exit_code)


if __name__ == "__main__":
    main()
