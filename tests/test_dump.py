"""Offline tests for the bulk-dump writer (no network)."""

from __future__ import annotations

import json

import pytest

from valleespyr.dump import dump_layer


class FakeClient:
    """Minimal stand-in for WFSClient: canned count + paged features."""

    def __init__(self, n_features: int, page_size_seen: list[int] | None = None):
        self._features = [
            {
                "type": "Feature",
                "id": f"f.{i}",
                "geometry": {"type": "Point", "coordinates": [i * 0.01, 42.0]},
                "properties": {"cleabs": f"BASSVERS{i:019d}", "toponyme": f"reach {i}"},
            }
            for i in range(n_features)
        ]
        self._seen = page_size_seen if page_size_seen is not None else []

    def count_features(self, layer, *, bbox=None, bbox_crs="EPSG:4326", cql_filter=None):
        return len(self._features)

    def iter_features(
        self,
        layer,
        *,
        page_size=1000,
        max_features=None,
        **kwargs,
    ):
        self._seen.append(page_size)
        out = self._features if max_features is None else self._features[:max_features]
        yield from out


def test_dump_geojson_roundtrip(tmp_path):
    out = tmp_path / "ws.geojson"
    n = dump_layer(FakeClient(7), "layer", out, page_size=1000)
    assert n == 7
    fc = json.loads(out.read_text())
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 7
    assert fc["features"][0]["properties"]["toponyme"] == "reach 0"


def test_dump_respects_max_features(tmp_path):
    out = tmp_path / "ws.geojson"
    n = dump_layer(FakeClient(100), "layer", out, max_features=10)
    assert n == 10
    assert len(json.loads(out.read_text())["features"]) == 10


def test_dump_progress_callback(tmp_path):
    calls: list[tuple[int, int | None]] = []
    dump_layer(
        FakeClient(3),
        "layer",
        tmp_path / "ws.geojson",
        progress=lambda n, total: calls.append((n, total)),
    )
    assert calls and calls[-1][0] == 3


def test_dump_geoparquet(tmp_path):
    gpd = pytest.importorskip("geopandas")
    pytest.importorskip("pyarrow")
    out = tmp_path / "ws.parquet"
    n = dump_layer(FakeClient(5), "layer", out, srs_name="EPSG:4326")
    assert n == 5
    gdf = gpd.read_parquet(out)
    assert len(gdf) == 5
    assert gdf.crs is not None and gdf.crs.to_epsg() == 4326


def test_dump_geoparquet_empty_raises(tmp_path):
    pytest.importorskip("geopandas")
    with pytest.raises(RuntimeError, match="no features"):
        dump_layer(FakeClient(0), "layer", tmp_path / "ws.parquet")
