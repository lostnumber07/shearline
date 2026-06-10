import pytest

from shearline.bounds import OutOfBoundsError, check_conus


@pytest.mark.parametrize(
    ("lat", "lon"),
    [
        (35.339, -97.487),  # Moore OK
        (43.914, -69.965),  # Brunswick ME
        (47.6, -122.33),  # Seattle
        (25.77, -80.19),  # Miami
    ],
)
def test_accepts_conus_points(lat, lon):
    check_conus(lat, lon)  # must not raise


@pytest.mark.parametrize(
    ("lat", "lon"),
    [
        (51.5, -0.12),  # London
        (21.3, -157.85),  # Honolulu
        (61.2, -149.9),  # Anchorage
        (35.339, 97.487),  # sign flip: eastern hemisphere
        (0, 0),
    ],
)
def test_rejects_out_of_bounds(lat, lon):
    with pytest.raises(OutOfBoundsError):
        check_conus(lat, lon)


def test_error_message_is_actionable():
    with pytest.raises(OutOfBoundsError, match="continental United States"):
        check_conus(35.339, 97.487)
