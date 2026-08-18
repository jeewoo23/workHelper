import json
from pathlib import Path

import pytest

from route_controller.directions import DirectionsCoordinate, GeneratedDirections
from route_controller.itinerary import (
    ItineraryPlace,
    ItineraryValidationError,
    NominatimGeocoder,
    OpenAIItineraryPlanner,
    compose_itinerary,
    itinerary_payload,
    parse_itinerary,
    parse_resolved_places,
)
from route_controller.routes import RouteRegistry
from route_controller.server import generate_itinerary_gpx, prepare_imported_gpx


ITINERARY = {
    "name": "Work day",
    "date": "2026-08-18",
    "timezone": "America/Los_Angeles",
    "places": [
        {"id": "home", "label": "Home", "query": "1 Home Street, San Jose CA"},
        {"id": "work", "label": "Work", "query": "1 Work Avenue, Sunnyvale CA"},
    ],
    "segments": [
        {
            "kind": "stay",
            "start": "2026-08-18T08:00:00",
            "end": "2026-08-18T09:00:00",
            "placeId": "home",
            "originId": None,
            "destinationId": None,
            "mode": "driving",
        },
        {
            "kind": "travel",
            "start": "2026-08-18T09:00:00",
            "end": "2026-08-18T10:00:00",
            "placeId": None,
            "originId": "home",
            "destinationId": "work",
            "mode": "driving",
        },
        {
            "kind": "stay",
            "start": "2026-08-18T10:00:00",
            "end": "2026-08-18T13:00:00",
            "placeId": "work",
            "originId": None,
            "destinationId": None,
            "mode": "driving",
        },
    ],
    "assumptions": ["Driving takes one hour"],
}


RESOLVED = [
    {
        "id": "home",
        "label": "Home",
        "query": "1 Home Street, San Jose CA",
        "latitude": 37.30,
        "longitude": -121.90,
        "displayName": "1 Home Street, San Jose, California",
    },
    {
        "id": "work",
        "label": "Work",
        "query": "1 Work Avenue, Sunnyvale CA",
        "latitude": 37.40,
        "longitude": -122.00,
        "displayName": "1 Work Avenue, Sunnyvale, California",
    },
]


class FakeDirectionsProvider:
    name = "Test Roads"

    def route(self, origin, destination):
        midpoint = DirectionsCoordinate(
            latitude=(origin.latitude + destination.latitude) / 2,
            longitude=(origin.longitude + destination.longitude) / 2,
        )
        return GeneratedDirections(
            provider=self.name,
            points=(origin, midpoint, destination),
            segment_durations_seconds=(30, 30),
            distance_meters=10_000,
            estimated_duration_seconds=60,
        )


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, maximum_bytes):
        return self.payload[:maximum_bytes]


def test_itinerary_round_trip_and_sparse_stationary_heartbeats() -> None:
    itinerary = parse_itinerary(ITINERARY)
    assert itinerary_payload(itinerary) == ITINERARY
    resolved = parse_resolved_places(RESOLVED, itinerary)

    composed = compose_itinerary(
        itinerary,
        resolved,
        directions_provider=FakeDirectionsProvider(),
    )

    assert len(composed.points) == 7
    assert [point.time.hour for point in composed.points] == [15, 16, 16, 17, 18, 19, 20]
    assert composed.points[2].time.minute == 30
    assert composed.points[-3].latitude == pytest.approx(37.40)
    assert composed.points[-2].latitude == pytest.approx(37.40)
    assert composed.points[-1].latitude == pytest.approx(37.40)
    assert composed.distance_meters == 10_000
    assert composed.travel_seconds == 3_600


def test_itinerary_rejects_schedule_gaps_and_location_jumps() -> None:
    with_gap = json.loads(json.dumps(ITINERARY))
    with_gap["segments"][1]["start"] = "2026-08-18T09:15:00"
    with pytest.raises(ItineraryValidationError, match="exactly"):
        parse_itinerary(with_gap)

    with_jump = json.loads(json.dumps(ITINERARY))
    with_jump["segments"][1]["originId"] = "work"
    with_jump["segments"][1]["destinationId"] = "home"
    with pytest.raises(ItineraryValidationError, match="continue"):
        parse_itinerary(with_jump)


def test_itinerary_requires_confirmation_coordinates_to_match() -> None:
    itinerary = parse_itinerary(ITINERARY)
    with pytest.raises(ItineraryValidationError, match="every place"):
        parse_resolved_places(RESOLVED[:1], itinerary)


def test_nominatim_geocoder_caches_and_identifies_requests(tmp_path: Path) -> None:
    requests = []

    def transport(request, *, timeout):
        requests.append(request)
        return FakeResponse(
            [
                {
                    "lat": "37.3",
                    "lon": "-121.9",
                    "display_name": "1 Home Street, San Jose, California",
                }
            ]
        )

    geocoder = NominatimGeocoder(
        tmp_path / "cache.json",
        transport=transport,
        min_interval_seconds=0,
    )
    place = ItineraryPlace("home", "Home", "1 Home Street, San Jose CA")

    first = geocoder.geocode(place)
    second = geocoder.geocode(place)

    assert first == second
    assert len(requests) == 1
    assert requests[0].get_header("User-agent").startswith("CentralBlue")
    assert "format=jsonv2" in requests[0].full_url
    assert json.loads((tmp_path / "cache.json").read_text())["1 home street, san jose ca"]


def test_openai_planner_requests_strict_structured_output() -> None:
    requests = []

    def transport(request, *, timeout):
        requests.append(request)
        return FakeResponse(
            {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": json.dumps(ITINERARY)}
                        ],
                    }
                ]
            }
        )

    planner = OpenAIItineraryPlanner("test-key", transport=transport)
    result = planner.interpret(
        "Home at 8, drive to work at 9 and stay until 1.",
        day="2026-08-18",
        timezone_name="America/Los_Angeles",
    )

    request_payload = json.loads(requests[0].data)
    assert result.name == "Work day"
    assert request_payload["model"] == "gpt-5.4-mini"
    assert request_payload["text"]["format"]["strict"] is True
    assert request_payload["text"]["format"]["schema"]["additionalProperties"] is False
    assert requests[0].get_header("Authorization") == "Bearer test-key"


def test_generated_itinerary_preserves_sparse_timing_when_prepared(tmp_path: Path) -> None:
    generated = generate_itinerary_gpx(
        {
            "confirmed": True,
            "itinerary": ITINERARY,
            "resolvedPlaces": RESOLVED,
        },
        imports_directory=tmp_path / "routes/imports",
        directions_provider=FakeDirectionsProvider(),
    )

    assert generated["sourceType"] == "llm-itinerary"
    assert generated["pointCount"] == 7
    assert generated["durationSeconds"] == 18_000
    assert generated["travelSeconds"] == 3_600

    prepared = prepare_imported_gpx(
        generated["filename"],
        {
            "durationSeconds": None,
            "timingMode": "auto",
            "label": "Work day",
        },
        imports_directory=tmp_path / "routes/imports",
        generated_directory=tmp_path / "routes/generated",
        registry=RouteRegistry(tmp_path),
    )

    assert prepared["timingMode"] == "source"
    assert prepared["pointCount"] == 7
    assert prepared["durationSeconds"] == 18_000
