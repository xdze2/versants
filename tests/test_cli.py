"""Tests for the ``valley catchments precompute`` CLI command.

The command's pure logic (skip-if-WFS-covered, skip-if-already-cached, the
area-range keep/discard decision, and the merged JSON shape) is exercised via
Click's ``CliRunner`` with ``valleespyr.cli._delineate_one_subprocess``
monkeypatched to a canned result — no real subprocess, DEM, OpenTopography or
pysheds call is made, so this runs without the ``dem`` extra or
``OPENTOPOGRAPHY_API_KEY``. Each river is delineated in its own subprocess in
production (see ``_delineate_one_subprocess``'s docstring) precisely so that
pysheds' occasional unrecoverable native crash only takes down that one
subprocess rather than the whole batch — that boundary is exactly the seam
these tests stub out.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

gpd = pytest.importorskip("geopandas")
pytest.importorskip("networkx")
pytest.importorskip("shapely")

from click.testing import CliRunner  # noqa: E402
from shapely.geometry import LineString, Polygon  # noqa: E402

from valleespyr.cli import cli  # noqa: E402


def _write_troncons(path: Path) -> None:
    """A tiny two-river network: Main <- Trib, as GeoJSON tronçons on disk."""
    rows = [
        ("MAIN_1", "N_s", "N_a", "2", "Main", "CDE_MAIN", [(-0.05, 42.80), (-0.03, 42.78)]),
        ("MAIN_2", "N_a", "N_o", "3", "Main", "CDE_MAIN", [(-0.03, 42.78), (0.02, 42.76)]),
        ("TRIB_1", "N_t", "N_a", "1", "Trib", "CDE_TRIB", [(-0.02, 42.82), (-0.03, 42.78)]),
    ]
    gdf = gpd.GeoDataFrame(
        {
            "cleabs": [r[0] for r in rows],
            "lien_vers_noeud_hydrographique_ini": [r[1] for r in rows],
            "lien_vers_noeud_hydrographique_fin": [r[2] for r in rows],
            "sens_de_l_ecoulement": ["Sens direct"] * len(rows),
            "numero_d_ordre": [r[3] for r in rows],
            "cpx_toponyme_de_cours_d_eau": [r[4] for r in rows],
            "liens_vers_cours_d_eau": [r[5] for r in rows],
            "fictif": [False] * len(rows),
            "reseau_principal_coulant": [True] * len(rows),
            "nature": ["Ecoulement naturel"] * len(rows),
            "geometry": [LineString(r[6]) for r in rows],
        },
        crs="EPSG:4326",
    )
    gdf.to_file(path, driver="GeoJSON")


def _canned_rings() -> list[list[list[float]]]:
    return [[[-0.05, 42.75], [0.03, 42.75], [0.03, 42.83], [-0.05, 42.83], [-0.05, 42.75]]]


def _canned_diag(area_km2: float) -> dict:
    return {
        "area_km2": area_km2,
        "snap_moved_cells": 0.4,
        "snap_xy": (-0.03, 42.78),
        "depression_fill_frac": 0.01,
        "n_cells": 1000,
        "course_inside_frac": 0.95,
        "outlet_lonlat": (-0.03, 42.78),
        "dem_bbox": (-0.05, 42.75, 0.03, 42.83),
        "dem_tif": "/tmp/fake.tif",
        "offset_m": 0,
        "acc_channel_cells": 200,
        "n_candidates": 1,
    }


def _patch_delineate_river(monkeypatch, area_by_id: dict[str, float]):
    """Stub cli._delineate_one_subprocess to hand back a canned result per river id.

    This is the boundary the real precompute loop crosses into a subprocess
    (see ``_delineate_one_subprocess``) — stubbing it here keeps these tests
    exercising the loop's own logic without spawning a real subprocess.
    """
    import valleespyr.cli as cli_mod

    def fake(river_id, **kwargs):
        area = area_by_id.get(river_id, 42.0)
        return (_canned_rings(), _canned_diag(area)), None

    monkeypatch.setattr(cli_mod, "_delineate_one_subprocess", fake)


# --------------------------------------------------------------------------- basics


def test_precompute_help():
    result = CliRunner().invoke(cli, ["valley", "catchments", "precompute", "--help"])
    assert result.exit_code == 0
    assert "DEM-delineate a catchment" in result.output


def test_precompute_defaults_to_s3_dem_source(tmp_path: Path, monkeypatch):
    """No API key, no rate limit by default: --dem-source defaults to 's3',
    and that choice must actually reach delineate_river."""
    troncons = tmp_path / "troncons.geojson"
    _write_troncons(troncons)
    out = tmp_path / "catchments.json"

    seen_sources: list[str] = []
    import valleespyr.cli as cli_mod

    def fake(river_id, **kwargs):
        seen_sources.append(kwargs.get("dem_source"))
        return (_canned_rings(), _canned_diag(60.0)), None

    monkeypatch.setattr(cli_mod, "_delineate_one_subprocess", fake)

    result = CliRunner().invoke(
        cli,
        ["valley", "catchments", "precompute", "CDE_MAIN", "--from-file", str(troncons), "-o", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert seen_sources and all(s == "s3" for s in seen_sources)


def test_precompute_dem_source_flag_is_passed_through(tmp_path: Path, monkeypatch):
    troncons = tmp_path / "troncons.geojson"
    _write_troncons(troncons)
    out = tmp_path / "catchments.json"

    seen_sources: list[str] = []
    import valleespyr.cli as cli_mod

    def fake(river_id, **kwargs):
        seen_sources.append(kwargs.get("dem_source"))
        return (_canned_rings(), _canned_diag(60.0)), None

    monkeypatch.setattr(cli_mod, "_delineate_one_subprocess", fake)

    result = CliRunner().invoke(
        cli,
        [
            "valley",
            "catchments",
            "precompute",
            "CDE_MAIN",
            "--from-file",
            str(troncons),
            "-o",
            str(out),
            "--dem-source",
            "opentopography",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen_sources and all(s == "opentopography" for s in seen_sources)


def test_precompute_writes_kept_rivers_in_area_range(tmp_path: Path, monkeypatch):
    troncons = tmp_path / "troncons.geojson"
    _write_troncons(troncons)
    out = tmp_path / "catchments.json"

    _patch_delineate_river(monkeypatch, {"CDE_MAIN": 60.0, "CDE_TRIB": 8.0})

    result = CliRunner().invoke(
        cli,
        [
            "valley",
            "catchments",
            "precompute",
            "CDE_MAIN",
            "--from-file",
            str(troncons),
            "-o",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output

    payload = json.loads(out.read_text("utf-8"))
    assert set(payload["rivers"]) == {"CDE_MAIN", "CDE_TRIB"}
    assert payload["rivers"]["CDE_MAIN"]["area_km2"] == 60.0
    assert payload["rivers"]["CDE_MAIN"]["source"] == "dem"
    assert payload["rivers"]["CDE_MAIN"]["catchment"]
    assert payload["meta"]["root_id"] == "CDE_MAIN"


def test_precompute_discards_out_of_range_area(tmp_path: Path, monkeypatch):
    troncons = tmp_path / "troncons.geojson"
    _write_troncons(troncons)
    out = tmp_path / "catchments.json"

    # CDE_TRIB comes back way too big (900 km²) -> discarded post-hoc
    _patch_delineate_river(monkeypatch, {"CDE_MAIN": 60.0, "CDE_TRIB": 900.0})

    result = CliRunner().invoke(
        cli,
        [
            "valley",
            "catchments",
            "precompute",
            "CDE_MAIN",
            "--from-file",
            str(troncons),
            "-o",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(out.read_text("utf-8"))
    # CDE_TRIB is recorded too (so a resume doesn't redo the DEM search for
    # it), but with no "catchment" - only CDE_MAIN got a real polygon.
    assert set(payload["rivers"]) == {"CDE_MAIN", "CDE_TRIB"}
    assert payload["rivers"]["CDE_MAIN"]["source"] == "dem"
    assert payload["rivers"]["CDE_TRIB"]["source"] == "discarded"
    assert "catchment" not in payload["rivers"]["CDE_TRIB"]
    assert "discarding" in result.output


def test_precompute_respects_custom_area_bounds(tmp_path: Path, monkeypatch):
    troncons = tmp_path / "troncons.geojson"
    _write_troncons(troncons)
    out = tmp_path / "catchments.json"

    _patch_delineate_river(monkeypatch, {"CDE_MAIN": 60.0, "CDE_TRIB": 8.0})

    result = CliRunner().invoke(
        cli,
        [
            "valley",
            "catchments",
            "precompute",
            "CDE_MAIN",
            "--from-file",
            str(troncons),
            "-o",
            str(out),
            "--area-min",
            "50",
            "--area-max",
            "70",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(out.read_text("utf-8"))
    # only CDE_MAIN (60 km²) now qualifies; CDE_TRIB (8 km²) is below --area-min
    # but is still recorded (as "discarded", no catchment) so a resume skips it.
    assert set(payload["rivers"]) == {"CDE_MAIN", "CDE_TRIB"}
    assert payload["rivers"]["CDE_TRIB"]["source"] == "discarded"


def test_precompute_is_incremental(tmp_path: Path, monkeypatch):
    troncons = tmp_path / "troncons.geojson"
    _write_troncons(troncons)
    out = tmp_path / "catchments.json"
    out.write_text(
        json.dumps(
            {
                "meta": {"root_id": "CDE_MAIN"},
                "rivers": {
                    "CDE_TRIB": {
                        "catchment": [[[0.0, 0.0], [0.0, 1.0], [1.0, 1.0], [0.0, 0.0]]],
                        "area_km2": 8.0,
                        "source": "dem",
                        "outlet_lonlat": [0.0, 0.0],
                        "snap_moved_cells": 0.1,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    calls: list[str] = []

    import valleespyr.cli as cli_mod

    def fake(river_id, **kwargs):
        calls.append(river_id)
        return (_canned_rings(), _canned_diag(60.0)), None

    monkeypatch.setattr(cli_mod, "_delineate_one_subprocess", fake)

    result = CliRunner().invoke(
        cli,
        [
            "valley",
            "catchments",
            "precompute",
            "CDE_MAIN",
            "--from-file",
            str(troncons),
            "-o",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    # CDE_TRIB was already cached -> delineate_river only called for CDE_MAIN
    assert calls == ["CDE_MAIN"]

    payload = json.loads(out.read_text("utf-8"))
    assert set(payload["rivers"]) == {"CDE_MAIN", "CDE_TRIB"}
    # the pre-existing entry is preserved untouched
    assert payload["rivers"]["CDE_TRIB"]["area_km2"] == 8.0


def test_precompute_skips_wfs_covered_rivers(tmp_path: Path, monkeypatch):
    troncons = tmp_path / "troncons.geojson"
    _write_troncons(troncons)
    out = tmp_path / "catchments.json"

    # Coverage is keyed to "this river + everything upstream of it"
    # (valleespyr.watershed.catchment_polygon), so covering CDE_TRIB (a leaf
    # with nothing upstream of it) leaves CDE_MAIN's own set ({CDE_MAIN,
    # CDE_TRIB}) matched too -- exactly like _valley_catchment's own semantics
    # elsewhere in this codebase. Both rivers are therefore WFS-covered here;
    # DEM must not run for either.
    bassins_path = tmp_path / "bassins.geojson"
    bassins = gpd.GeoDataFrame(
        {
            "liens_vers_cours_d_eau_principal": ["CDE_TRIB"],
            "geometry": [Polygon([(-0.03, 42.79), (-0.02, 42.79), (-0.02, 42.81), (-0.03, 42.81)])],
        },
        crs="EPSG:4326",
    )
    bassins.to_file(bassins_path, driver="GeoJSON")

    calls: list[str] = []
    import valleespyr.cli as cli_mod

    def fake(river_id, **kwargs):
        calls.append(river_id)
        return (_canned_rings(), _canned_diag(60.0)), None

    monkeypatch.setattr(cli_mod, "_delineate_one_subprocess", fake)

    result = CliRunner().invoke(
        cli,
        [
            "valley",
            "catchments",
            "precompute",
            "CDE_MAIN",
            "--from-file",
            str(troncons),
            "--bassins",
            str(bassins_path),
            "-o",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == []
    assert "skipped (WFS-covered)" in result.output

    payload = json.loads(out.read_text("utf-8"))
    assert payload["rivers"] == {}


def test_precompute_skips_degenerate_bbox_without_calling_delineate(tmp_path: Path, monkeypatch):
    """A near-point tronçon (e.g. a ~20m stray fragment, real case: 'Ruisseau
    d'Areng' COURDEAU0000002491710079) has a catchment_bbox too narrow for any
    DEM tile to crop — rasterio errors with 'Attempt to create 1x0 dataset'
    once you actually try. That must be caught before spawning the delineate
    subprocess at all (recorded as undetermined, not a crash to keep
    retrying), since no bbox this narrow can ever be delineated."""
    troncons = tmp_path / "troncons.geojson"
    rows = [
        ("MAIN_1", "N_s", "N_a", "2", "Main", "CDE_MAIN", [(-0.05, 42.80), (-0.03, 42.78)]),
        ("MAIN_2", "N_a", "N_o", "3", "Main", "CDE_MAIN", [(-0.03, 42.78), (0.02, 42.76)]),
        # A ~20m fragment: both endpoints round to the same DEM pixel.
        ("TRIB_1", "N_t", "N_a", "1", "Trib", "CDE_TRIB", [(-0.03005, 42.78002), (-0.03000, 42.78000)]),
    ]
    gdf = gpd.GeoDataFrame(
        {
            "cleabs": [r[0] for r in rows],
            "lien_vers_noeud_hydrographique_ini": [r[1] for r in rows],
            "lien_vers_noeud_hydrographique_fin": [r[2] for r in rows],
            "sens_de_l_ecoulement": ["Sens direct"] * len(rows),
            "numero_d_ordre": [r[3] for r in rows],
            "cpx_toponyme_de_cours_d_eau": [r[4] for r in rows],
            "liens_vers_cours_d_eau": [r[5] for r in rows],
            "fictif": [False] * len(rows),
            "reseau_principal_coulant": [True] * len(rows),
            "nature": ["Ecoulement naturel"] * len(rows),
            "geometry": [LineString(r[6]) for r in rows],
        },
        crs="EPSG:4326",
    )
    gdf.to_file(troncons, driver="GeoJSON")
    out = tmp_path / "catchments.json"

    calls: list[str] = []
    import valleespyr.cli as cli_mod

    def fake(river_id, **kwargs):
        calls.append(river_id)
        return (_canned_rings(), _canned_diag(60.0)), None

    monkeypatch.setattr(cli_mod, "_delineate_one_subprocess", fake)

    result = CliRunner().invoke(
        cli,
        [
            "valley",
            "catchments",
            "precompute",
            "CDE_MAIN",
            "--from-file",
            str(troncons),
            "-o",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    # the degenerate CDE_TRIB never reaches _delineate_one_subprocess
    assert calls == ["CDE_MAIN"]
    assert "too narrow" in result.output

    payload = json.loads(out.read_text("utf-8"))
    assert payload["rivers"]["CDE_TRIB"]["source"] == "undetermined"


def test_precompute_continues_after_one_river_fails(tmp_path: Path, monkeypatch):
    """A river whose subprocess dies (crash, or any uncaught exception inside
    it — see ``_delineate_one_subprocess``) is logged as a warning and
    skipped, not resolved; the batch continues to the next river rather than
    aborting."""
    troncons = tmp_path / "troncons.geojson"
    _write_troncons(troncons)
    out = tmp_path / "catchments.json"

    import valleespyr.cli as cli_mod

    def fake(river_id, **kwargs):
        if river_id == "CDE_TRIB":
            return None, "subprocess exit 1: RuntimeError: empty catchment"
        return (_canned_rings(), _canned_diag(60.0)), None

    monkeypatch.setattr(cli_mod, "_delineate_one_subprocess", fake)

    result = CliRunner().invoke(
        cli,
        [
            "valley",
            "catchments",
            "precompute",
            "CDE_MAIN",
            "--from-file",
            str(troncons),
            "-o",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(out.read_text("utf-8"))
    assert set(payload["rivers"]) == {"CDE_MAIN"}
    assert "warning" in result.output


def test_precompute_stops_and_saves_on_opentopography_rate_limit(tmp_path: Path, monkeypatch):
    """OpenTopography's daily-quota rejection comes back as an HTTP 401 (not a
    429) with an XML body saying 'API maximum rate limit reached' — this must
    stop the batch (no point trying the next river) but still write out
    whatever was already collected, so the run can resume later.

    The real worker subprocess lets that ``HTTPError`` propagate uncaught, so
    its message ends up in the subprocess's stderr tail that
    ``_delineate_one_subprocess`` folds into ``crash_msg`` — this test stubs
    that boundary directly with the same text."""
    troncons = tmp_path / "troncons.geojson"
    _write_troncons(troncons)
    out = tmp_path / "catchments.json"

    import valleespyr.cli as cli_mod

    def fake(river_id, **kwargs):
        if river_id == "CDE_TRIB":
            return None, (
                "subprocess exit 1: requests.exceptions.HTTPError: 401 Client "
                "Error — response body: <error>Error: API maximum rate limit "
                "reached. (50 API calls/24hrs)</error>"
            )
        return (_canned_rings(), _canned_diag(60.0)), None

    monkeypatch.setattr(cli_mod, "_delineate_one_subprocess", fake)

    result = CliRunner().invoke(
        cli,
        [
            "valley",
            "catchments",
            "precompute",
            "CDE_MAIN",
            "--from-file",
            str(troncons),
            "-o",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "stopped early" in result.output
    assert "rate limit" in result.output
    # CDE_MAIN was kept before the rate limit hit on CDE_TRIB; that result
    # must still be on disk, not lost to the aborted run.
    payload = json.loads(out.read_text("utf-8"))
    assert set(payload["rivers"]) == {"CDE_MAIN"}


def test_precompute_skips_river_on_non_rate_limit_http_error(tmp_path: Path, monkeypatch):
    """A non-rate-limit HTTPError (e.g. a real auth failure fetching one
    river's tile) is a per-river problem, not a batch-wide one — it must be
    logged and skipped like any other single-river failure, not abort the run
    (a real terrain batch over ~600 rivers will hit odd failures; one bad
    river shouldn't cost every result after it)."""
    troncons = tmp_path / "troncons.geojson"
    _write_troncons(troncons)
    out = tmp_path / "catchments.json"

    import valleespyr.cli as cli_mod

    def fake(river_id, **kwargs):
        if river_id == "CDE_TRIB":
            return None, (
                "subprocess exit 1: requests.exceptions.HTTPError: "
                "401 Client Error — response body: <error>Error: invalid API key</error>"
            )
        return (_canned_rings(), _canned_diag(60.0)), None

    monkeypatch.setattr(cli_mod, "_delineate_one_subprocess", fake)

    result = CliRunner().invoke(
        cli,
        [
            "valley",
            "catchments",
            "precompute",
            "CDE_MAIN",
            "--from-file",
            str(troncons),
            "-o",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "warning" in result.output
    payload = json.loads(out.read_text("utf-8"))
    assert set(payload["rivers"]) == {"CDE_MAIN"}


def test_precompute_skips_river_on_unexpected_error(tmp_path: Path, monkeypatch):
    """A real batch over ~600 rivers of actual terrain will hit edge cases
    (e.g. an empty flow-accumulation channel mask raising a raw IndexError
    deep inside pysheds, or the subprocess dying to a native segfault
    entirely) that aren't RuntimeError or HTTPError. One river's oddity must
    not sink the whole run."""
    troncons = tmp_path / "troncons.geojson"
    _write_troncons(troncons)
    out = tmp_path / "catchments.json"

    import valleespyr.cli as cli_mod

    def fake(river_id, **kwargs):
        if river_id == "CDE_TRIB":
            return None, "subprocess exit -6: double free or corruption (out)"
        return (_canned_rings(), _canned_diag(60.0)), None

    monkeypatch.setattr(cli_mod, "_delineate_one_subprocess", fake)

    result = CliRunner().invoke(
        cli,
        [
            "valley",
            "catchments",
            "precompute",
            "CDE_MAIN",
            "--from-file",
            str(troncons),
            "-o",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "warning" in result.output
    payload = json.loads(out.read_text("utf-8"))
    assert set(payload["rivers"]) == {"CDE_MAIN"}


def _write_troncons_three_rivers(path: Path) -> None:
    """Main <- TribA, TribB (both joining Main): three independently
    delineate-able rivers, for a test that needs more than one to actually
    get 'kept' before an unrecoverable failure."""
    rows = [
        ("MAIN_1", "N_s", "N_o", "3", "Main", "CDE_MAIN", [(-0.05, 42.80), (0.02, 42.76)]),
        ("TA_1", "N_a", "N_s", "1", "TribA", "CDE_TRIBA", [(-0.02, 42.84), (-0.05, 42.80)]),
        ("TB_1", "N_b", "N_s", "1", "TribB", "CDE_TRIBB", [(-0.08, 42.82), (-0.05, 42.80)]),
    ]
    gdf = gpd.GeoDataFrame(
        {
            "cleabs": [r[0] for r in rows],
            "lien_vers_noeud_hydrographique_ini": [r[1] for r in rows],
            "lien_vers_noeud_hydrographique_fin": [r[2] for r in rows],
            "sens_de_l_ecoulement": ["Sens direct"] * len(rows),
            "numero_d_ordre": [r[3] for r in rows],
            "cpx_toponyme_de_cours_d_eau": [r[4] for r in rows],
            "liens_vers_cours_d_eau": [r[5] for r in rows],
            "fictif": [False] * len(rows),
            "reseau_principal_coulant": [True] * len(rows),
            "nature": ["Ecoulement naturel"] * len(rows),
            "geometry": [LineString(r[6]) for r in rows],
        },
        crs="EPSG:4326",
    )
    gdf.to_file(path, driver="GeoJSON")


def test_precompute_saves_incrementally_before_an_unrecoverable_crash(
    tmp_path: Path, monkeypatch
):
    """A batch over hundreds of rivers runs long enough to hit a failure no
    Python except could catch in-process — pysheds' native 'double free or
    corruption' crash. That's exactly why each river is delineated in its own
    subprocess (see ``_delineate_one_subprocess``): such a crash now only
    kills that one subprocess, surfacing to the loop as an ordinary
    ``crash_msg`` rather than taking the whole batch down with it.

    This test simulates the one failure mode that *can* still kill the parent
    outright (e.g. the process being killed, an OOM) with a ``BaseException``
    raised directly in the loop, after the first river was already kept and
    saved — proving the incremental ``_write_out()`` after every resolved
    river protects prior progress even then, not just against the isolated
    per-river crashes ``_delineate_one_subprocess`` now absorbs."""
    troncons = tmp_path / "troncons.geojson"
    _write_troncons_three_rivers(troncons)
    out = tmp_path / "catchments.json"

    kept_so_far: list[str] = []
    import valleespyr.cli as cli_mod

    def fake(river_id, **kwargs):
        if len(kept_so_far) >= 1:
            raise BaseException("simulated parent-process kill (e.g. OOM)")  # noqa: TRY002
        kept_so_far.append(river_id)
        return (_canned_rings(), _canned_diag(60.0)), None

    monkeypatch.setattr(cli_mod, "_delineate_one_subprocess", fake)

    with pytest.raises(BaseException, match="simulated parent-process kill"):
        CliRunner().invoke(
            cli,
            [
                "valley",
                "catchments",
                "precompute",
                "CDE_MAIN",
                "--from-file",
                str(troncons),
                "-o",
                str(out),
            ],
            catch_exceptions=False,
        )

    # the first river's result must be on disk even though the run never
    # reached its own final _write_out() call after the loop
    payload = json.loads(out.read_text("utf-8"))
    assert set(payload["rivers"]) == set(kept_so_far)
    assert len(kept_so_far) == 1
