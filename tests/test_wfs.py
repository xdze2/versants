"""Offline unit tests for the WFS client (no network)."""

from __future__ import annotations

import pytest

from valleespyr.sources.wfs import WFSClient, WFSError, _parse_exception

CAPS = b"""<?xml version="1.0"?>
<wfs:WFS_Capabilities xmlns:wfs="http://www.opengis.net/wfs/2.0"
                      xmlns:ows="http://www.opengis.net/ows/1.1" version="2.0.0">
  <wfs:FeatureTypeList>
    <wfs:FeatureType>
      <wfs:Name>BDTOPO_V3:bassin_versant_topographique</wfs:Name>
      <wfs:Title>BDTOPO : Bassins versants topographiques</wfs:Title>
      <wfs:Abstract>Bassins versants topographiques</wfs:Abstract>
      <wfs:DefaultCRS>urn:ogc:def:crs:EPSG::4326</wfs:DefaultCRS>
      <ows:WGS84BoundingBox>
        <ows:LowerCorner>-5.5 41.2</ows:LowerCorner>
        <ows:UpperCorner>9.7 51.3</ows:UpperCorner>
      </ows:WGS84BoundingBox>
    </wfs:FeatureType>
    <wfs:FeatureType>
      <wfs:Name>BDTOPO_V3:troncon_de_route</wfs:Name>
      <wfs:Title>Troncons de route</wfs:Title>
    </wfs:FeatureType>
  </wfs:FeatureTypeList>
</wfs:WFS_Capabilities>
"""

EXCEPTION = b"""<?xml version="1.0"?>
<ows:ExceptionReport xmlns:ows="http://www.opengis.net/ows/1.1" version="2.0.0">
  <ows:Exception exceptionCode="InvalidParameterValue" locator="TYPENAMES">
    <ows:ExceptionText>Unknown type name</ows:ExceptionText>
  </ows:Exception>
</ows:ExceptionReport>
"""


class FakeResp:
    def __init__(self, content: bytes, ctype: str = "application/xml", json_obj=None):
        self.content = content
        self.text = content.decode("utf-8", "replace")
        self.headers = {"Content-Type": ctype}
        self._json = json_obj

    def raise_for_status(self):
        pass

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def test_list_feature_types_parses_and_filters(monkeypatch):
    client = WFSClient()
    monkeypatch.setattr(client, "_get", lambda params: FakeResp(CAPS))

    all_fts = client.list_feature_types()
    assert {ft.name for ft in all_fts} == {
        "BDTOPO_V3:bassin_versant_topographique",
        "BDTOPO_V3:troncon_de_route",
    }

    hits = client.list_feature_types(keyword="bassin")
    assert len(hits) == 1
    ft = hits[0]
    assert ft.title.startswith("BDTOPO")
    assert ft.wgs84_bbox == (-5.5, 41.2, 9.7, 51.3)


def test_get_feature_rejects_bbox_and_cql_together():
    client = WFSClient()
    with pytest.raises(ValueError):
        client.get_feature("x", bbox=(0, 0, 1, 1), cql_filter="INTERSECTS(g, POINT(0 0))")


def test_exception_report_raises_wfserror(monkeypatch):
    client = WFSClient()

    def fake_request(url, params, timeout):  # pragma: no cover - shape only
        raise AssertionError("should not be called")

    monkeypatch.setattr(
        client.session, "get", lambda *a, **k: FakeResp(EXCEPTION)
    )
    with pytest.raises(WFSError) as ei:
        client._get({"REQUEST": "GetFeature"})
    assert "InvalidParameterValue" in str(ei.value)


def test_parse_exception_plain_text_fallback():
    assert "boom" in _parse_exception(b"not xml boom")
