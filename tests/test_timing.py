import json
from datetime import datetime, timedelta, timezone

import pytest

from route_controller.timing import (
    OsrmRouteTimingProvider,
    RoadTimingEstimate,
    TimingCoordinate,
    TimingProfileError,
    build_timing_plan,
)


POINTS = [
    TimingCoordinate(latitude=37.0, longitude=-122.0),
    TimingCoordinate(latitude=37.0, longitude=-121.99),
    TimingCoordinate(latitude=37.0, longitude=-121.98),
]


class FakeRoadProvider:
    name = "Test Roads"

    def __init__(self, durations=(10.0, 30.0), *, error=None):
        self.durations = durations
        self.error = error
        self.calls = 0

    def estimate(self, points):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return RoadTimingEstimate(
            provider=self.name,
            segment_durations_seconds=tuple(self.durations),
            estimated_duration_seconds=sum(self.durations),
            anchor_count=len(points),
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


def test_route_aware_plan_preserves_relative_road_speeds_when_scaled() -> None:
    provider = FakeRoadProvider()

    plan = build_timing_plan(
        POINTS,
        requested_duration_seconds=80,
        provider=provider,
    )

    assert plan.mode == "route-aware"
    assert plan.provider == "Test Roads"
    assert plan.estimated_duration_seconds == 40
    assert plan.offsets_seconds == (0, 20, 80)
    assert provider.calls == 1


def test_auto_plan_prefers_complete_source_timestamps() -> None:
    started_at = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    provider = FakeRoadProvider(error=AssertionError("provider should not run"))

    plan = build_timing_plan(
        POINTS,
        requested_duration_seconds=80,
        source_times=[
            started_at,
            started_at + timedelta(seconds=10),
            started_at + timedelta(seconds=40),
        ],
        provider=provider,
    )

    assert plan.mode == "source"
    assert plan.offsets_seconds == (0, 20, 80)
    assert plan.estimated_duration_seconds == 40
    assert provider.calls == 0


def test_auto_plan_uses_uniform_fallback_for_custom_duration() -> None:
    provider = FakeRoadProvider(error=TimingProfileError("provider offline"))

    plan = build_timing_plan(
        POINTS,
        requested_duration_seconds=100,
        provider=provider,
    )

    assert plan.mode == "uniform"
    assert plan.duration_seconds == 100
    assert "provider offline" in plan.warning


def test_auto_eta_requires_provider_when_source_is_untimed() -> None:
    provider = FakeRoadProvider(error=TimingProfileError("provider offline"))

    with pytest.raises(TimingProfileError, match="Road-aware timing is unavailable"):
        build_timing_plan(
            POINTS,
            requested_duration_seconds=None,
            provider=provider,
        )


def test_osrm_adapter_maps_anchor_leg_durations_to_source_segments() -> None:
    requested_urls = []

    def fake_transport(request, *, timeout):
        requested_urls.append((request.full_url, timeout))
        return FakeResponse(
            {
                "code": "Ok",
                "routes": [
                    {
                        "legs": [
                            {"duration": 20.0},
                            {"duration": 5.0},
                        ]
                    }
                ],
            }
        )

    provider = OsrmRouteTimingProvider(
        "https://routing.example",
        max_waypoints=3,
        transport=fake_transport,
    )
    estimate = provider.estimate(
        [
            TimingCoordinate(latitude=37.0, longitude=-122.00),
            TimingCoordinate(latitude=37.0, longitude=-121.99),
            TimingCoordinate(latitude=37.0, longitude=-121.98),
            TimingCoordinate(latitude=37.0, longitude=-121.97),
        ]
    )

    assert estimate.provider == "OSRM"
    assert estimate.anchor_count == 3
    assert estimate.estimated_duration_seconds == pytest.approx(25)
    assert estimate.segment_durations_seconds == pytest.approx((10, 10, 5))
    assert requested_urls[0][0].startswith(
        "https://routing.example/route/v1/driving/"
    )
    assert requested_urls[0][1] == 12
