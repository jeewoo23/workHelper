import pytest
from urllib.error import HTTPError

import route_controller.google_maps as google_maps
from route_controller.directions import DirectionsCoordinate
from route_controller.google_maps import (
    GoogleMapsGeocodingRequiredError,
    GoogleMapsLinkResolutionError,
    GoogleMapsLinkValidationError,
    expand_google_maps_short_url,
    resolve_google_maps_directions_link,
)


def test_resolves_official_coordinate_directions_url() -> None:
    link = resolve_google_maps_directions_link(
        "https://www.google.com/maps/dir/?api=1"
        "&origin=37.4158495%2C-122.0349283"
        "&destination=37.3920662%2C-122.0947471"
    )

    assert link.origin == DirectionsCoordinate(37.4158495, -122.0349283)
    assert link.destination == DirectionsCoordinate(37.3920662, -122.0947471)
    assert link.was_shortened is False
    assert link.resolved_host == "www.google.com"


def test_resolves_coordinate_endpoints_from_shared_path() -> None:
    link = resolve_google_maps_directions_link(
        "https://www.google.com/maps/dir/"
        "37.4158495,-122.0349283/37.3920662,-122.0947471/"
        "@37.4,-122.05,12z/data=!4m2"
    )

    assert link.origin.latitude == 37.4158495
    assert link.destination.longitude == -122.0947471


def test_resolves_coordinates_embedded_in_named_shared_path() -> None:
    link = resolve_google_maps_directions_link(
        "https://www.google.com/maps/dir/Stanford/UC+Berkeley/"
        "data=!4m2!3d37.4275000!4d-122.1697000"
        "!3d37.8715000!4d-122.2730000"
    )

    assert link.origin == DirectionsCoordinate(37.4275, -122.1697)
    assert link.destination == DirectionsCoordinate(37.8715, -122.273)


def test_short_link_uses_injected_expander() -> None:
    expanded_values = []

    def expand(value: str) -> str:
        expanded_values.append(value)
        return (
            "https://www.google.com/maps/dir/?api=1"
            "&origin=37.4,-122.0&destination=37.5,-122.1"
        )

    link = resolve_google_maps_directions_link(
        "https://maps.app.goo.gl/abc123",
        expander=expand,
    )

    assert expanded_values == ["https://maps.app.goo.gl/abc123"]
    assert link.was_shortened is True
    assert link.destination == DirectionsCoordinate(37.5, -122.1)


def test_place_names_require_phase_four_geocoding() -> None:
    with pytest.raises(GoogleMapsGeocodingRequiredError, match="place names"):
        resolve_google_maps_directions_link(
            "https://www.google.com/maps/dir/?api=1"
            "&origin=Stanford+University&destination=UC+Berkeley"
        )


def test_short_link_expansion_refuses_redirect_outside_google(
    monkeypatch,
) -> None:
    class FakeOpener:
        def open(self, request, timeout):
            raise HTTPError(
                request.full_url,
                302,
                "Found",
                {"Location": "https://example.com/private"},
                None,
            )

    monkeypatch.setattr(
        google_maps,
        "build_opener",
        lambda handler: FakeOpener(),
    )

    with pytest.raises(
        GoogleMapsLinkResolutionError,
        match="outside Google",
    ):
        expand_google_maps_short_url("https://maps.app.goo.gl/abc123")


@pytest.mark.parametrize(
    "value",
    [
        "http://www.google.com/maps/dir/?api=1&origin=1,2&destination=3,4",
        "https://example.com/maps/dir/?api=1&origin=1,2&destination=3,4",
        "https://www.google.com/maps/search/coffee",
        "https://user@example.com/maps/dir/?api=1&origin=1,2&destination=3,4",
    ],
)
def test_rejects_unsafe_or_non_directions_urls(value: str) -> None:
    with pytest.raises(GoogleMapsLinkValidationError):
        resolve_google_maps_directions_link(value)
