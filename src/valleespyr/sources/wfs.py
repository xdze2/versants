"""Minimal OGC WFS 2.0 client, tuned for exploring BD TOPAGE / BD TOPO watershed data.

The Géoplateforme serves topographic watersheds as ``BDTOPO_V3:bassin_versant_topographique``
on the WFS endpoint ``https://data.geopf.fr/wfs/ows``. The full BD TOPAGE dataset is also
published by Sandre; both speak WFS 2.0, so the endpoint and layer name are configurable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET

import requests

# Default: IGN Géoplateforme WFS.
GEOPLATEFORME_WFS = "https://data.geopf.fr/wfs/ows"

# Topographic watershed layer on the Géoplateforme (BD TOPO v3, same concept as BD TOPAGE).
LAYER_BASSIN_VERSANT = "BDTOPO_V3:bassin_versant_topographique"

_WFS_NS = {
    "wfs": "http://www.opengis.net/wfs/2.0",
    "ows": "http://www.opengis.net/ows/1.1",
    "fes": "http://www.opengis.net/fes/2.0",
}


class WFSError(RuntimeError):
    """Raised when the WFS server returns an OGC ExceptionReport or an unexpected payload."""


@dataclass
class FeatureType:
    name: str
    title: str = ""
    abstract: str = ""
    default_crs: str = ""
    wgs84_bbox: tuple[float, float, float, float] | None = None


@dataclass
class WFSClient:
    endpoint: str = GEOPLATEFORME_WFS
    version: str = "2.0.0"
    timeout: float = 60.0
    session: requests.Session = field(default_factory=requests.Session)

    # -- low level ---------------------------------------------------------------

    def _get(self, params: dict[str, Any]) -> requests.Response:
        base = {"SERVICE": "WFS", "VERSION": self.version}
        base.update(params)
        resp = self.session.get(self.endpoint, params=base, timeout=self.timeout)
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "")
        # OGC servers return 200 with an ExceptionReport body on logical errors.
        if "xml" in ctype and b"ExceptionReport" in resp.content[:2000]:
            raise WFSError(_parse_exception(resp.content))
        return resp

    # -- capabilities ---------------------------------------------------------------

    def list_feature_types(self, keyword: str | None = None) -> list[FeatureType]:
        """Return advertised feature types, optionally filtered by a case-insensitive keyword."""
        resp = self._get({"REQUEST": "GetCapabilities"})
        root = ET.fromstring(resp.content)
        out: list[FeatureType] = []
        for ft in root.iterfind(".//wfs:FeatureTypeList/wfs:FeatureType", _WFS_NS):
            name = _text(ft, "wfs:Name")
            if not name:
                continue
            bbox = None
            lc = _text(ft, "ows:WGS84BoundingBox/ows:LowerCorner")
            uc = _text(ft, "ows:WGS84BoundingBox/ows:UpperCorner")
            if lc and uc:
                minx, miny = (float(v) for v in lc.split())
                maxx, maxy = (float(v) for v in uc.split())
                bbox = (minx, miny, maxx, maxy)
            out.append(
                FeatureType(
                    name=name,
                    title=_text(ft, "wfs:Title"),
                    abstract=_text(ft, "wfs:Abstract"),
                    default_crs=_text(ft, "wfs:DefaultCRS"),
                    wgs84_bbox=bbox,
                )
            )
        if keyword:
            k = keyword.lower()
            out = [
                ft
                for ft in out
                if k in ft.name.lower() or k in ft.title.lower() or k in ft.abstract.lower()
            ]
        return out

    def describe_feature_type(self, type_name: str) -> list[tuple[str, str]]:
        """Return ``(field_name, field_type)`` pairs for a layer's schema."""
        resp = self._get({"REQUEST": "DescribeFeatureType", "TYPENAMES": type_name})
        root = ET.fromstring(resp.content)
        xsd = "http://www.w3.org/2001/XMLSchema"
        fields: list[tuple[str, str]] = []
        for el in root.iterfind(f".//{{{xsd}}}sequence/{{{xsd}}}element"):
            fields.append((el.get("name", ""), el.get("type", "")))
        return fields

    # -- features ---------------------------------------------------------------

    def get_feature(
        self,
        type_name: str,
        *,
        bbox: tuple[float, float, float, float] | None = None,
        bbox_crs: str = "EPSG:4326",
        cql_filter: str | None = None,
        count: int | None = None,
        start_index: int | None = None,
        srs_name: str = "EPSG:4326",
        output_format: str = "application/json",
        property_name: str | list[str] | None = None,
    ) -> dict[str, Any]:
        """Fetch features as GeoJSON. ``bbox`` and ``cql_filter`` are mutually exclusive."""
        if bbox is not None and cql_filter is not None:
            raise ValueError("Pass either bbox or cql_filter, not both (WFS restriction).")

        params: dict[str, Any] = {
            "REQUEST": "GetFeature",
            "TYPENAMES": type_name,
            "SRSNAME": srs_name,
            "OUTPUTFORMAT": output_format,
        }
        if bbox is not None:
            params["BBOX"] = ",".join(str(c) for c in (*bbox, bbox_crs))
        if cql_filter is not None:
            params["CQL_FILTER"] = cql_filter
        if count is not None:
            params["COUNT"] = count
        if start_index is not None:
            params["STARTINDEX"] = start_index
        if property_name is not None:
            if isinstance(property_name, (list, tuple)):
                property_name = ",".join(property_name)
            params["PROPERTYNAME"] = property_name

        resp = self._get(params)
        try:
            return resp.json()
        except ValueError as exc:  # not JSON -> surface the body for debugging
            raise WFSError(f"Expected GeoJSON, got {resp.headers.get('Content-Type')}: "
                           f"{resp.text[:500]}") from exc

    def count_features(
        self,
        type_name: str,
        *,
        bbox: tuple[float, float, float, float] | None = None,
        bbox_crs: str = "EPSG:4326",
        cql_filter: str | None = None,
    ) -> int:
        """Return the total number of matching features (RESULTTYPE=hits)."""
        params: dict[str, Any] = {
            "REQUEST": "GetFeature",
            "TYPENAMES": type_name,
            "RESULTTYPE": "hits",
        }
        if bbox is not None:
            params["BBOX"] = ",".join(str(c) for c in (*bbox, bbox_crs))
        if cql_filter is not None:
            params["CQL_FILTER"] = cql_filter
        resp = self._get(params)
        root = ET.fromstring(resp.content)
        n = root.get("numberMatched") or root.get("numberOfFeatures")
        if n is None or n == "unknown":
            raise WFSError("Server did not report numberMatched for a hits query.")
        return int(n)

    def iter_features(
        self,
        type_name: str,
        *,
        page_size: int = 1000,
        max_features: int | None = None,
        **kwargs: Any,
    ):
        """Yield GeoJSON features, paging with STARTINDEX/COUNT until exhausted."""
        fetched = 0
        start = 0
        while True:
            page = self.get_feature(
                type_name, count=page_size, start_index=start, **kwargs
            )
            feats = page.get("features", [])
            if not feats:
                break
            for f in feats:
                yield f
                fetched += 1
                if max_features is not None and fetched >= max_features:
                    return
            if len(feats) < page_size:
                break
            start += page_size


def _text(el: ET.Element, path: str) -> str:
    found = el.find(path, _WFS_NS)
    return (found.text or "").strip() if found is not None and found.text else ""


def _parse_exception(content: bytes) -> str:
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return content.decode("utf-8", "replace")[:500]
    parts = [
        (e.text or "").strip()
        for e in root.iterfind(".//ows:ExceptionText", _WFS_NS)
    ]
    code = ""
    exc = root.find(".//ows:Exception", _WFS_NS)
    if exc is not None:
        code = exc.get("exceptionCode", "")
    return f"WFS exception [{code}]: " + " | ".join(p for p in parts if p)
