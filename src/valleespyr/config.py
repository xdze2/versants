"""Load ``config/study_area.yaml`` into the params threaded through the CLI.

The study area config is the source of truth for the WFS dump extent (stage
1) and the list of root rivers each get their own catalog build (stage 5) —
see ``app_design.md``'s "Study area config" note. This module only parses it
into plain values; wiring those values into the CLI's option defaults and
into the Makefile is separate.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("config/study_area.yaml")


@dataclass(frozen=True)
class RootRiver:
    """One entry in ``roots``: a river to build a catalog for."""

    name: str
    cours_d_eau_id: str | None

    @property
    def query(self) -> str:
        """The value to pass as ``valleespyr catalog``'s ``ROOT_QUERY``.

        Prefers the resolved id (unambiguous) over the name (a substring
        match that can hit more than one river).
        """
        if self.cours_d_eau_id is None:
            raise ValueError(
                f"root {self.name!r} has no cours_d_eau_id yet — resolve it in "
                "config/study_area.yaml before using it as a catalog root"
            )
        return self.cours_d_eau_id

    @property
    def slug(self) -> str:
        """A filesystem/URL-safe stand-in for ``name`` (e.g. "l'Adour" -> "adour").

        Used to give each root's build its own output path under ``docs/``.
        """
        normalized = unicodedata.normalize("NFKD", self.name)
        ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
        return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")


@dataclass(frozen=True)
class StudyArea:
    """Parsed ``study_area.yaml``: dump extent, CRS, WFS sources, catalog roots."""

    name: str
    bbox_wgs84: tuple[float, float, float, float]
    crs: str
    wfs_endpoint: str
    troncon_layer: str
    watershed_layer: str
    roots: list[RootRiver]

    @property
    def bbox_str(self) -> str:
        """The bbox formatted as the CLI's ``minx,miny,maxx,maxy`` string."""
        return ",".join(str(c) for c in self.bbox_wgs84)

    def resolved_roots(self) -> list[RootRiver]:
        """Roots whose ``cours_d_eau_id`` has been filled in (usable today)."""
        return [r for r in self.roots if r.cours_d_eau_id is not None]


def load_study_area(path: str | Path = DEFAULT_CONFIG_PATH) -> StudyArea:
    """Parse a ``study_area.yaml`` file into a :class:`StudyArea`."""
    path = Path(path)
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return _from_dict(data, path)


def load_study_area_if_present(path: str | Path = DEFAULT_CONFIG_PATH) -> StudyArea | None:
    """Like :func:`load_study_area`, but ``None`` if ``path`` doesn't exist.

    Used to pick CLI option defaults: fall back to the built-in constants
    when there's no study area config (e.g. outside this project's repo)
    rather than failing every command.
    """
    path = Path(path)
    if not path.is_file():
        return None
    return load_study_area(path)


def _from_dict(data: dict[str, Any], path: Path) -> StudyArea:
    bbox = data["bbox_wgs84"]
    if len(bbox) != 4:
        raise ValueError(f"{path}: bbox_wgs84 must have exactly 4 values, got {bbox!r}")

    sources = data.get("sources", {})
    roots = [
        RootRiver(name=r["name"], cours_d_eau_id=r.get("cours_d_eau_id"))
        for r in data.get("roots", [])
    ]

    return StudyArea(
        name=data["name"],
        bbox_wgs84=tuple(float(c) for c in bbox),  # type: ignore[arg-type]
        crs=data["crs"],
        wfs_endpoint=sources["wfs_endpoint"],
        troncon_layer=sources["troncon_layer"],
        watershed_layer=sources["watershed_layer"],
        roots=roots,
    )
