"""Tests for parsing config/study_area.yaml."""

from __future__ import annotations

from pathlib import Path

import pytest

from valleespyr.config import (
    RootRiver,
    load_study_area,
    load_valley_overrides,
    load_valley_overrides_if_present,
)

_YAML = """
name: test-area
bbox_wgs84: [-1.5, 42.0, 3.5, 43.5]
crs: EPSG:2154
sources:
  wfs_endpoint: https://example.org/wfs
  troncon_layer: BDTOPO_V3:troncon_hydrographique
  watershed_layer: BDTOPO_V3:bassin_versant_topographique
roots:
  - name: la Garonne
    cours_d_eau_id: null
  - name: Neste de Rioumajou
    cours_d_eau_id: COURDEAU0000002000907013
"""


def _write(tmp_path: Path, text: str = _YAML) -> Path:
    path = tmp_path / "study_area.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_study_area_parses_fields(tmp_path: Path) -> None:
    area = load_study_area(_write(tmp_path))

    assert area.name == "test-area"
    assert area.bbox_wgs84 == (-1.5, 42.0, 3.5, 43.5)
    assert area.crs == "EPSG:2154"
    assert area.wfs_endpoint == "https://example.org/wfs"
    assert area.troncon_layer == "BDTOPO_V3:troncon_hydrographique"
    assert area.watershed_layer == "BDTOPO_V3:bassin_versant_topographique"
    assert area.bbox_str == "-1.5,42.0,3.5,43.5"


def test_load_study_area_parses_roots(tmp_path: Path) -> None:
    area = load_study_area(_write(tmp_path))

    assert area.roots == [
        RootRiver(name="la Garonne", cours_d_eau_id=None),
        RootRiver(name="Neste de Rioumajou", cours_d_eau_id="COURDEAU0000002000907013"),
    ]
    resolved = area.resolved_roots()
    assert len(resolved) == 1
    assert resolved[0].query == "COURDEAU0000002000907013"


def test_root_query_raises_when_unresolved(tmp_path: Path) -> None:
    area = load_study_area(_write(tmp_path))
    unresolved = next(r for r in area.roots if r.cours_d_eau_id is None)
    with pytest.raises(ValueError, match="no cours_d_eau_id"):
        _ = unresolved.query


def test_load_study_area_rejects_bad_bbox(tmp_path: Path) -> None:
    bad = _YAML.replace("[-1.5, 42.0, 3.5, 43.5]", "[-1.5, 42.0, 3.5]")
    with pytest.raises(ValueError, match="4 values"):
        load_study_area(_write(tmp_path, bad))


def test_the_real_project_config_loads() -> None:
    """config/study_area.yaml itself should always parse."""
    area = load_study_area(Path(__file__).resolve().parent.parent / "config" / "study_area.yaml")
    assert area.name == "pyrenees-garonne"
    assert len(area.roots) == 2


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("la Garonne", "la-garonne"),
        ("l'Adour", "l-adour"),
        ("Neste de Rioumajou", "neste-de-rioumajou"),
        ("l'Échez", "l-echez"),
    ],
)
def test_root_river_slug(name: str, slug: str) -> None:
    assert RootRiver(name=name, cours_d_eau_id="x").slug == slug


_OVERRIDES_YAML = """
rivers:
  COURDEAU0000002000905987:
    camera:
      azimuth_deg: 200
      elevation_deg: 25
      distance_m: 4000
      target_height_m: 250
  COURDEAU0000002000000001:
    blacklist: true
"""


def test_load_valley_overrides_parses_rivers(tmp_path: Path) -> None:
    path = tmp_path / "valley_overrides.yaml"
    path.write_text(_OVERRIDES_YAML, encoding="utf-8")

    overrides = load_valley_overrides(path)

    assert overrides["COURDEAU0000002000905987"]["camera"]["azimuth_deg"] == 200
    assert overrides["COURDEAU0000002000000001"]["blacklist"] is True


def test_load_valley_overrides_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "valley_overrides.yaml"
    path.write_text("rivers: {}\n", encoding="utf-8")

    assert load_valley_overrides(path) == {}


def test_load_valley_overrides_if_present_missing(tmp_path: Path) -> None:
    assert load_valley_overrides_if_present(tmp_path / "nope.yaml") is None


def test_the_real_project_valley_overrides_loads() -> None:
    """config/valley_overrides.yaml itself should always parse."""
    path = Path(__file__).resolve().parent.parent / "config" / "valley_overrides.yaml"
    overrides = load_valley_overrides(path)
    assert "COURDEAU0000002000905987" in overrides
