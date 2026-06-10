import pytest

from shearline.geo import compass_point, distance_bearing, haversine_km, initial_bearing_deg


def test_haversine_known_distance():
    # Moore OK -> Norman OK is roughly 13 km
    d = haversine_km(35.339, -97.487, 35.2226, -97.4395)
    assert 12 < d < 15


def test_haversine_zero():
    assert haversine_km(35.0, -97.0, 35.0, -97.0) == 0


def test_bearing_cardinal_directions():
    assert initial_bearing_deg(35.0, -97.0, 36.0, -97.0) == pytest.approx(0, abs=1)  # north
    assert initial_bearing_deg(35.0, -97.0, 34.0, -97.0) == pytest.approx(180, abs=1)  # south
    assert initial_bearing_deg(35.0, -97.0, 35.0, -96.0) == pytest.approx(90, abs=2)  # east


@pytest.mark.parametrize(
    ("deg", "name"),
    [(0, "N"), (45, "NE"), (90, "E"), (180, "S"), (270, "W"), (340, "NNW"), (359, "N")],
)
def test_compass_point(deg, name):
    assert compass_point(deg) == name


def test_distance_bearing_shape():
    out = distance_bearing(35.0, -97.0, 36.0, -97.0)
    assert set(out) == {"distance_km", "bearing_deg", "direction"}
    assert out["direction"] == "N"
    assert 110 < out["distance_km"] < 112.5
