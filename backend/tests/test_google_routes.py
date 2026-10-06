import io
import json
import urllib.error

import pytest

from notana_planner.domain import Location
from notana_planner.travel import google_routes as G


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _locs(n=3):
    return [Location(f"l{i}", 59.3 + i * 0.01, 18.0, "Z") for i in range(n)]


def _ok(req, timeout=None):
    body = json.loads(req.data)
    els = [
        {"originIndex": o, "destinationIndex": d, "condition": "ROUTE_EXISTS", "duration": "600s", "distanceMeters": 5000}
        for o in range(len(body["origins"])) for d in range(len(body["destinations"]))
    ]
    return _Resp(json.dumps(els).encode())


def _http(code):
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b'{"error":"x"}'))


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setattr(G.time, "sleep", lambda s: None)
    return G.GoogleRoutesTravelTimeProvider(api_key="test", cache_dir=tmp_path, pace_s=0)


def test_retries_rate_limit_then_succeeds_and_caches(provider, monkeypatch, tmp_path):
    calls = {"n": 0}

    def flaky(req, timeout=None):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _http(429)
        return _ok(req)

    monkeypatch.setattr(G.urllib.request, "urlopen", flaky)
    m = provider.build_matrix(_locs())
    assert m.source == "google-routes"
    assert abs(m.minutes[0][1] - (10 + 3)) < 1e-9  # 600 s + door overhead
    assert list(tmp_path.glob("google_matrix_*.json"))


def test_rejected_key_falls_back_and_is_reported(provider, monkeypatch, tmp_path):
    def denied(req, timeout=None):
        raise _http(403)

    monkeypatch.setattr(G.urllib.request, "urlopen", denied)
    m = provider.build_matrix(_locs())
    assert m.source.startswith("synthetic")
    assert any("rejected" in n for n in m.notes)
    assert not list(tmp_path.glob("google_matrix_*.json"))  # never cache a fallback


def test_road_geometry_straight_without_key_and_parsed_with_key(monkeypatch):
    from notana_planner.travel import road_geometry as R

    pts = [(59.30, 18.0), (59.31, 18.01), (59.31, 18.01), (59.32, 18.02)]
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    assert R.road_legs(pts)["source"] == "straight"

    def fake_post(body, key, timeout=20.0):
        n = len(body["intermediates"]) + 1
        leg = {"polyline": {"geoJsonLinestring": {"coordinates": [[18.0, 59.3], [18.005, 59.305]]}},
               "duration": "120s", "distanceMeters": 900}
        return {"routes": [{"legs": [leg] * n}]}

    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "k")
    monkeypatch.setattr(R, "_post", fake_post)
    R._legs_cached.cache_clear()
    out = R.road_legs(pts)
    assert out["source"] == "google-routes"
    assert len(out["legs"]) == 3
    assert out["legs"][0]["path"][0] == [59.3, 18.0]  # lat/lng order restored
    assert out["legs"][1]["distance_m"] == 0  # duplicate point: no request
