import json

import pytest

from route_controller.directions import (
    DirectionsCoordinate,
    DirectionsProviderError,
    DirectionsValidationError,
    GeneratedDirections,
    OsrmDirectionsProvider,
    generate_route,
)


ORIGIN = DirectionsCoordinate(latitude=37.4158495, longitude=-122.0349283)
DESTINATION = DirectionsCoordinate(latitude=37.3920662, longitude=-122.0947471)


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, maximum_bytes):
        return self.payload[:maximum_bytes]


def test_osrm_directions_returns_full_geometry_and_edge_timing() -> None:
    requests = []

    def fake_transport(request, *, timeout):
        requests.append((request.full_url, timeout))
        return FakeResponse(
            {
                "code": "Ok",
                "routes": [
                    {
                        "distance": 1500.0,
                        "duration": 40.0,
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [
                                [-122.0349283, 37.4158495],
                                [-122.0500000, 37.4050000],
                                [-122.0947471, 37.3920662],
                            ],
                        },
                        "legs": [
                            {
                                "annotation": {
                                    "duration": [10.0, 30.0],
                                    "distance": [400.0, 1100.0],
                                }
                            }
                        ],
                    }
                ],
            }
        )

    provider = OsrmDirectionsProvider(
        "https://routing.example",
        transport=fake_transport,
    )
    generated = generate_route(ORIGIN, DESTINATION, provider=provider)

    assert generated.provider == "OSRM"
    assert generated.points[0] == ORIGIN
    assert generated.points[-1] == DESTINATION
    assert generated.distance_meters == 1500
    assert generated.estimated_duration_seconds == 40
    assert generated.segment_durations_seconds == pytest.approx((10, 30))
    assert requests[0][0].startswith(
        "https://routing.example/route/v1/driving/"
        "-122.0349283,37.4158495;-122.0947471,37.3920662?"
    )
    assert "overview=full" in requests[0][0]
    assert "geometries=geojson" in requests[0][0]
    assert requests[0][1] == 15


def test_osrm_directions_rejects_no_route_response() -> None:
    provider = OsrmDirectionsProvider(
        transport=lambda request, *, timeout: FakeResponse(
            {"code": "NoRoute", "message": "Impossible route"}
        )
    )

    with pytest.raises(DirectionsProviderError, match="Impossible route"):
        generate_route(ORIGIN, DESTINATION, provider=provider)


def test_generate_route_rejects_nearly_identical_coordinates() -> None:
    class ProviderThatMustNotRun:
        name = "unused"

        def route(self, origin, destination):
            raise AssertionError("provider should not run")

    with pytest.raises(DirectionsValidationError, match="at least 5 meters"):
        generate_route(
            ORIGIN,
            DirectionsCoordinate(
                latitude=ORIGIN.latitude,
                longitude=ORIGIN.longitude,
            ),
            provider=ProviderThatMustNotRun(),
        )


def test_generate_route_validates_provider_geometry_shape() -> None:
    class InvalidProvider:
        name = "invalid"

        def route(self, origin, destination):
            return GeneratedDirections(
                provider=self.name,
                points=(origin, destination),
                segment_durations_seconds=(10.0, 20.0),
                distance_meters=100,
                estimated_duration_seconds=30,
            )

    with pytest.raises(DirectionsProviderError, match="timing"):
        generate_route(ORIGIN, DESTINATION, provider=InvalidProvider())
