from datetime import timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from route_controller.gpx import (
    GpxValidationError,
    RoutePoint,
    generate_directional_tracks,
    parse_track,
    parse_xcode_waypoints,
    split_round_trip,
    summarize,
    validate_points,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "routes" / "source" / "route_final.gpx"


def test_source_route_has_expected_split_and_duration() -> None:
    points = parse_xcode_waypoints(SOURCE)
    outbound, inbound = split_round_trip(points)

    assert len(points) == 93
    assert len(outbound) == 47
    assert len(inbound) == 47
    assert outbound[0].name == "L1"
    assert outbound[-1].name == "L2"
    assert inbound[0].name == "L2"
    assert inbound[-1].name == "L1 return"
    assert summarize("outbound", outbound).duration_seconds == 1200
    assert summarize("inbound", inbound).duration_seconds == 1200


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
            == 47
        )

        name, points = parse_track(path)
        assert name == expected_name
        assert summarize(name, points).duration_seconds == 1200


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
