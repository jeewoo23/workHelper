from datetime import timedelta
from math import atan2, cos, radians, sin, sqrt
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from route_controller.gpx import (
    GpxValidationError,
    RoutePoint,
    generate_directional_tracks,
    interpolate_points,
    parse_track,
    parse_xcode_waypoints,
    split_round_trip,
    summarize,
    validate_points,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "routes" / "source" / "route_final.gpx"
HIGHWAY_START = (37.40382498413415, -122.02724763671414)
HIGHWAY_END = (37.3920662232116, -122.09474709677077)


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
