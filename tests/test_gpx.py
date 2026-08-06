from datetime import datetime, timedelta, timezone
from math import atan2, cos, radians, sin, sqrt
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from route_controller.gpx import (
    GpxValidationError,
    RoutePoint,
    generate_directional_tracks,
    inspect_gpx_content,
    interpolate_points,
    parse_track,
    prepare_gpx_playback,
    prepare_gpx_playback_result,
    parse_xcode_waypoints,
    split_round_trip,
    summarize,
    validate_points,
)
from route_controller.timing import RoadTimingEstimate

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "routes" / "source" / "route_final.gpx"
HIGHWAY_START = (37.40382498413415, -122.02724763671414)
HIGHWAY_END = (37.3920662232116, -122.09474709677077)
UNTIMED_TRACK = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="MapsToGPX"
     xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <name>Imported drive</name>
    <trkseg>
      <trkpt lat="37.3835546" lon="-122.1371287"/>
      <trkpt lat="37.41584954048625" lon="-122.03492834466675"/>
    </trkseg>
  </trk>
</gpx>
"""


def _distance_meters(
    first: tuple[float, float], second: tuple[float, float]
) -> float:
    lat1, lon1 = map(radians, first)
    lat2, lon2 = map(radians, second)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    haversine = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 6371000 * 2 * atan2(sqrt(haversine), sqrt(1 - haversine))


def _nearest_index(points: list[RoutePoint], target: tuple[float, float]) -> int:
    return min(
        range(len(points)),
        key=lambda index: _distance_meters(
            (points[index].latitude, points[index].longitude), target
        ),
    )


def test_source_route_has_expected_split_and_duration() -> None:
    points = parse_xcode_waypoints(SOURCE)
    outbound, inbound = split_round_trip(points)

    assert len(points) == 2064
    assert len(outbound) == 995
    assert len(inbound) == 1070
    assert outbound[0].name == "L1"
    assert outbound[-1].name == "L2"
    assert inbound[0].name == "L2"
    assert inbound[-1].name == "L1 return"
    assert summarize("outbound", outbound).duration_seconds == 1200
    assert summarize("inbound", inbound).duration_seconds == 1200


def test_highway_sections_are_timed_faster_in_both_directions() -> None:
    points = parse_xcode_waypoints(SOURCE)
    outbound, inbound = split_round_trip(points)

    outbound_start = _nearest_index(outbound, HIGHWAY_END)
    outbound_end = _nearest_index(outbound, HIGHWAY_START)
    inbound_start = _nearest_index(inbound, HIGHWAY_START)
    inbound_end = _nearest_index(inbound, HIGHWAY_END)

    outbound_duration = (
        outbound[outbound_end].time - outbound[outbound_start].time
    ).total_seconds()
    inbound_duration = (
        inbound[inbound_end].time - inbound[inbound_start].time
    ).total_seconds()

    assert outbound_start < outbound_end
    assert inbound_start < inbound_end
    assert round(outbound_duration, 1) == 309.9
    assert round(inbound_duration) == 290


def test_generated_files_are_single_timed_tracks(tmp_path: Path) -> None:
    outbound_path, inbound_path = generate_directional_tracks(SOURCE, tmp_path)

    for path, expected_name in (
        (outbound_path, "L1 to L2"),
        (inbound_path, "L2 to L1"),
    ):
        root = ET.parse(path).getroot()
        assert root.tag.endswith("gpx")
        assert not any(element.tag.endswith("wpt") for element in root.iter())
        assert len([element for element in root.iter() if element.tag.endswith("trk")]) == 1
        assert (
            len([element for element in root.iter() if element.tag.endswith("trkseg")])
            == 1
        )
        assert (
            len([element for element in root.iter() if element.tag.endswith("trkpt")])
            == (995 if expected_name == "L1 to L2" else 1070)
        )

        name, points = parse_track(path)
        assert name == expected_name
        assert summarize(name, points).duration_seconds == 1200


def test_interpolation_adds_linear_points_and_preserves_endpoints() -> None:
    points = parse_xcode_waypoints(SOURCE)
    outbound, _ = split_round_trip(points)

    interpolated = interpolate_points(outbound, 1)

    assert interpolated[0] == outbound[0]
    assert interpolated[-1] == outbound[-1]
    assert interpolated[-1].name == "L2"
    assert summarize("outbound", interpolated).duration_seconds == 1200
    assert len(interpolated) >= 1201


def test_interpolated_generated_tracks_have_one_second_samples(
    tmp_path: Path,
) -> None:
    outbound_path, inbound_path = generate_directional_tracks(
        SOURCE, tmp_path, interpolate_seconds=1
    )

    outbound_name, outbound = parse_track(outbound_path)
    inbound_name, inbound = parse_track(inbound_path)

    assert outbound_name == "L1 to L2"
    assert inbound_name == "L2 to L1"
    assert len(outbound) >= 1201
    assert len(inbound) >= 1201
    assert outbound[-1].name == "L2"
    assert inbound[0].name == "L2"
    assert summarize(outbound_name, outbound).duration_seconds == 1200
    assert summarize(inbound_name, inbound).duration_seconds == 1200


def test_interpolated_generated_tracks_can_use_half_second_samples(
    tmp_path: Path,
) -> None:
    outbound_path, inbound_path = generate_directional_tracks(
        SOURCE, tmp_path, interpolate_seconds=0.5
    )

    outbound_name, outbound = parse_track(outbound_path)
    inbound_name, inbound = parse_track(inbound_path)

    assert len(outbound) >= 2401
    assert len(inbound) >= 2401
    assert summarize(outbound_name, outbound).duration_seconds == 1200
    assert summarize(inbound_name, inbound).duration_seconds == 1200
    assert outbound[1].time - outbound[0].time == timedelta(seconds=0.5)


def test_non_monotonic_timestamp_is_rejected() -> None:
    points = parse_xcode_waypoints(SOURCE)[:2]
    invalid = [
        points[0],
        RoutePoint(
            latitude=points[1].latitude,
            longitude=points[1].longitude,
            time=points[0].time - timedelta(seconds=1),
        ),
    ]

    with pytest.raises(GpxValidationError, match="precedes"):
        validate_points(invalid)


def test_split_requires_exactly_one_named_midpoint() -> None:
    points = parse_xcode_waypoints(SOURCE)

    with pytest.raises(GpxValidationError, match="exactly one"):
        split_round_trip(points, split_name="missing")


def test_import_inspection_accepts_untimed_track_geometry() -> None:
    summary = inspect_gpx_content(UNTIMED_TRACK)

    assert summary.name == "Imported drive"
    assert summary.geometry_type == "track"
    assert summary.point_count == 2
    assert summary.timestamped_point_count == 0
    assert summary.segment_count == 1
    assert summary.start == (37.3835546, -122.1371287)
    assert summary.end == (37.41584954048625, -122.03492834466675)


def test_import_inspection_rejects_unsafe_xml_declarations() -> None:
    content = """<?xml version="1.0"?>
    <!DOCTYPE gpx [<!ENTITY route "unsafe">]>
    <gpx version="1.1"><wpt lat="1" lon="2"/><wpt lat="3" lon="4"/></gpx>
    """

    with pytest.raises(GpxValidationError, match="not allowed"):
        inspect_gpx_content(content)


def test_untimed_import_can_be_prepared_as_half_second_playback() -> None:
    started_at = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)

    name, points = prepare_gpx_playback(
        UNTIMED_TRACK,
        duration_seconds=120,
        interpolate_seconds=0.5,
        start_time=started_at,
    )

    assert name == "Imported drive"
    assert points[0].time == started_at
    assert points[0].latitude == 37.3835546
    assert points[-1].latitude == 37.41584954048625
    assert summarize(name, points).duration_seconds == 120
    assert len(points) == 241
    assert points[1].time - points[0].time == timedelta(seconds=0.5)


def test_timed_import_preserves_relative_timing_when_rescaled() -> None:
    content = """<?xml version="1.0"?>
    <gpx version="1.1">
      <trk><name>Timed route</name><trkseg>
        <trkpt lat="1" lon="1"><time>2026-01-01T12:00:00Z</time></trkpt>
        <trkpt lat="2" lon="2"><time>2026-01-01T12:00:10Z</time></trkpt>
        <trkpt lat="3" lon="3"><time>2026-01-01T12:00:30Z</time></trkpt>
      </trkseg></trk>
    </gpx>
    """
    started_at = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)

    _, points = prepare_gpx_playback(
        content,
        duration_seconds=60,
        interpolate_seconds=None,
        start_time=started_at,
    )

    assert points[1].time - points[0].time == timedelta(seconds=20)
    assert points[2].time - points[0].time == timedelta(seconds=60)


def test_dense_timed_import_is_resampled_to_strict_half_seconds() -> None:
    content = SOURCE.read_text(encoding="utf-8")
    started_at = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)

    prepared = prepare_gpx_playback_result(
        content,
        duration_seconds=10,
        interpolate_seconds=0.5,
        start_time=started_at,
        timing_mode="source",
    )

    assert len(prepared.points) == 21
    assert prepared.points[0].time == started_at
    assert prepared.points[-1].time == started_at + timedelta(seconds=10)
    assert prepared.points[0].latitude == pytest.approx(37.3835546)
    assert prepared.points[-1].latitude == pytest.approx(37.3835546)
    assert all(
        second.time - first.time == timedelta(seconds=0.5)
        for first, second in zip(prepared.points, prepared.points[1:])
    )


def test_duplicate_source_timestamps_do_not_reach_prepared_track() -> None:
    content = """<?xml version="1.0"?>
    <gpx version="1.1">
      <trk><name>Duplicate source time</name><trkseg>
        <trkpt lat="37.0" lon="-122.0">
          <time>2026-01-01T12:00:00Z</time>
        </trkpt>
        <trkpt lat="37.0001" lon="-122.0001">
          <time>2026-01-01T12:00:00Z</time>
        </trkpt>
        <trkpt lat="37.0002" lon="-122.0002">
          <time>2026-01-01T12:00:02Z</time>
        </trkpt>
      </trkseg></trk>
    </gpx>
    """

    prepared = prepare_gpx_playback_result(
        content,
        duration_seconds=10,
        interpolate_seconds=0.5,
        timing_mode="source",
    )

    assert len(prepared.points) == 21
    assert all(
        second.time > first.time
        for first, second in zip(prepared.points, prepared.points[1:])
    )


def test_untimed_import_uses_route_aware_segment_profile() -> None:
    content = """<?xml version="1.0"?>
    <gpx version="1.1">
      <trk><name>Road profile</name><trkseg>
        <trkpt lat="37.0" lon="-122.00"/>
        <trkpt lat="37.0" lon="-121.99"/>
        <trkpt lat="37.0" lon="-121.98"/>
      </trkseg></trk>
    </gpx>
    """

    class FakeProvider:
        name = "Test Roads"

        def estimate(self, points):
            return RoadTimingEstimate(
                provider=self.name,
                segment_durations_seconds=(10.0, 30.0),
                estimated_duration_seconds=40.0,
                anchor_count=3,
            )

    prepared = prepare_gpx_playback_result(
        content,
        duration_seconds=80,
        interpolate_seconds=None,
        timing_provider=FakeProvider(),
    )

    assert prepared.timing.mode == "route-aware"
    assert prepared.timing.provider == "Test Roads"
    assert prepared.points[1].time - prepared.points[0].time == timedelta(
        seconds=20
    )
    assert prepared.points[2].time - prepared.points[1].time == timedelta(
        seconds=60
    )

    resampled = prepare_gpx_playback_result(
        content,
        duration_seconds=80,
        interpolate_seconds=0.5,
        start_time=datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc),
        timing_provider=FakeProvider(),
    )
    assert len(resampled.points) == 161
    assert resampled.points[40].longitude == pytest.approx(-121.99)
    assert resampled.points[40].time - resampled.points[0].time == timedelta(
        seconds=20
    )
