from notana_planner.domain import Location
from notana_planner.geo import haversine_km
from notana_planner.travel import SyntheticTravelTimeProvider, TrafficConditions, TravelMatrix


def _locs():
    return {
        "a": Location("a", 59.3150, 18.0710, "Södermalm"),
        "b": Location("b", 59.3200, 18.0750, "Södermalm"),
        "c": Location("c", 59.3380, 17.9400, "Bromma"),
    }


def test_haversine_known_distance():
    # Stockholm Central -> Uppsala Central is ~64 km great-circle.
    assert 62 <= haversine_km(59.3303, 18.0586, 59.8586, 17.6389) <= 66


def test_synthetic_is_symmetric_and_grows_with_distance():
    locs = _locs()
    m = SyntheticTravelTimeProvider().build_matrix(list(locs.values()))
    tm = TravelMatrix(m, locs)
    assert tm.minutes("a", "a") == 0
    assert tm.minutes("a", "b") == tm.minutes("b", "a")
    assert tm.minutes("a", "c") > tm.minutes("a", "b") > 0
    assert tm.source == "synthetic"


def test_traffic_multipliers_apply_to_zone_and_global():
    locs = _locs()
    base = SyntheticTravelTimeProvider().build_matrix(list(locs.values()))
    tm = TravelMatrix(base, locs)
    zone = tm.with_traffic(TrafficConditions(zone_multipliers={"Bromma": 2.0}))
    assert zone.minutes("a", "c") >= 2 * tm.minutes("a", "c") - 1
    assert zone.minutes("a", "b") == tm.minutes("a", "b")
    glob = tm.with_traffic(TrafficConditions(global_multiplier=1.5))
    assert glob.minutes("a", "b") >= tm.minutes("a", "b")
