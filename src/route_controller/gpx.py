from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Sequence
from xml.etree import ElementTree as ET

GPX_NAMESPACE = "http://www.topografix.com/GPX/1/1"
ET.register_namespace("", GPX_NAMESPACE)


class GpxValidationError(ValueError):
    """Raised when a route cannot be safely converted or played."""


@dataclass(frozen=True)
class RoutePoint:
    latitude: float
    longitude: float
    time: datetime
    name: Optional[str] = None


@dataclass(frozen=True)
class RouteSummary:
    name: str
    point_count: int
    started_at: datetime
    ended_at: datetime
    duration_seconds: float
    start: RoutePoint
    end: RoutePoint


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local_name(child.tag) == name]


def _first_child(element: ET.Element, name: str) -> Optional[ET.Element]:
    return next((child for child in element if _local_name(child.tag) == name), None)


def _required_text(element: ET.Element, name: str) -> str:
    child = _first_child(element, name)
    if child is None or child.text is None or not child.text.strip():
        raise GpxValidationError(f"GPX point is missing a {name!r} value")
    return child.text.strip()


def _parse_time(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise GpxValidationError(f"Invalid GPX timestamp: {value}") from error
    if parsed.tzinfo is None:
        raise GpxValidationError(f"GPX timestamp has no timezone: {value}")
    return parsed


def _parse_point(element: ET.Element) -> RoutePoint:
    try:
        latitude = float(element.attrib["lat"])
        longitude = float(element.attrib["lon"])
    except (KeyError, ValueError) as error:
        raise GpxValidationError("GPX point has invalid latitude/longitude") from error

    name_element = _first_child(element, "name")
    name = (
        name_element.text.strip()
        if name_element is not None and name_element.text and name_element.text.strip()
        else None
    )
    return RoutePoint(
        latitude=latitude,
        longitude=longitude,
        time=_parse_time(_required_text(element, "time")),
        name=name,
    )


def validate_points(points: Sequence[RoutePoint]) -> None:
    if not points:
        raise GpxValidationError("Route contains no points")

    previous_time: Optional[datetime] = None
    for index, point in enumerate(points):
        if not -90 <= point.latitude <= 90:
            raise GpxValidationError(
                f"Point {index} latitude is outside [-90, 90]: {point.latitude}"
            )
        if not -180 <= point.longitude <= 180:
            raise GpxValidationError(
                f"Point {index} longitude is outside [-180, 180]: {point.longitude}"
            )
        if previous_time is not None and point.time < previous_time:
            raise GpxValidationError(
                f"Point {index} timestamp precedes the previous point"
            )
        previous_time = point.time


def parse_xcode_waypoints(path: Path) -> list[RoutePoint]:
    root = ET.parse(path).getroot()
    points = [_parse_point(element) for element in _children(root, "wpt")]
    validate_points(points)
    return points


def parse_track(path: Path) -> tuple[str, list[RoutePoint]]:
    root = ET.parse(path).getroot()
    tracks = _children(root, "trk")
    if len(tracks) != 1:
        raise GpxValidationError(
            f"Expected exactly one GPX track, found {len(tracks)}"
        )

    track = tracks[0]
    name_element = _first_child(track, "name")
    name = (
        name_element.text.strip()
        if name_element is not None and name_element.text and name_element.text.strip()
        else path.stem
    )
    segments = _children(track, "trkseg")
    if len(segments) != 1:
        raise GpxValidationError(
            f"Expected exactly one track segment, found {len(segments)}"
        )
    points = [_parse_point(element) for element in _children(segments[0], "trkpt")]
    validate_points(points)
    return name, points


def split_round_trip(
    points: Sequence[RoutePoint], split_name: str = "L2"
) -> tuple[list[RoutePoint], list[RoutePoint]]:
    matches = [index for index, point in enumerate(points) if point.name == split_name]
    if len(matches) != 1:
        raise GpxValidationError(
            f"Expected exactly one point named {split_name!r}, found {len(matches)}"
        )
    split_index = matches[0]
    outbound = list(points[: split_index + 1])
    inbound = list(points[split_index:])
    validate_points(outbound)
    validate_points(inbound)
    return outbound, inbound


def interpolate_points(
    points: Sequence[RoutePoint], interval_seconds: int
) -> list[RoutePoint]:
    validate_points(points)
    if interval_seconds <= 0:
        raise GpxValidationError(
            f"Interpolation interval must be positive: {interval_seconds}"
        )

    interpolated = [points[0]]
    interval = timedelta(seconds=interval_seconds)
    for start, end in zip(points, points[1:]):
        segment_duration = end.time - start.time
        if segment_duration.total_seconds() <= 0:
            if end.time == start.time:
                interpolated.append(end)
                continue
            raise GpxValidationError("Route segment has a negative duration")

        cursor = start.time + interval
        while cursor < end.time:
            fraction = (cursor - start.time).total_seconds() / segment_duration.total_seconds()
            interpolated.append(
                RoutePoint(
                    latitude=start.latitude
                    + ((end.latitude - start.latitude) * fraction),
                    longitude=start.longitude
                    + ((end.longitude - start.longitude) * fraction),
                    time=cursor,
                )
            )
            cursor += interval
        interpolated.append(end)

    validate_points(interpolated)
    return interpolated


def _format_time(value: datetime) -> str:
    utc_text = value.isoformat(timespec="seconds")
    return utc_text.replace("+00:00", "Z")


def write_track(path: Path, name: str, points: Sequence[RoutePoint]) -> None:
    validate_points(points)
    root = ET.Element(
        f"{{{GPX_NAMESPACE}}}gpx",
        {"version": "1.1", "creator": "iPhone Route Controller"},
    )
    track = ET.SubElement(root, f"{{{GPX_NAMESPACE}}}trk")
    ET.SubElement(track, f"{{{GPX_NAMESPACE}}}name").text = name
    segment = ET.SubElement(track, f"{{{GPX_NAMESPACE}}}trkseg")

    for point in points:
        element = ET.SubElement(
            segment,
            f"{{{GPX_NAMESPACE}}}trkpt",
            {"lat": str(point.latitude), "lon": str(point.longitude)},
        )
        if point.name:
            ET.SubElement(element, f"{{{GPX_NAMESPACE}}}name").text = point.name
        ET.SubElement(element, f"{{{GPX_NAMESPACE}}}time").text = _format_time(
            point.time
        )

    ET.indent(root, space="  ")
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def summarize(name: str, points: Sequence[RoutePoint]) -> RouteSummary:
    validate_points(points)
    duration = (points[-1].time - points[0].time).total_seconds()
    return RouteSummary(
        name=name,
        point_count=len(points),
        started_at=points[0].time,
        ended_at=points[-1].time,
        duration_seconds=duration,
        start=points[0],
        end=points[-1],
    )


def generate_directional_tracks(
    source: Path,
    output_directory: Path,
    split_name: str = "L2",
    interpolate_seconds: Optional[int] = None,
) -> tuple[Path, Path]:
    points = parse_xcode_waypoints(source)
    outbound, inbound = split_round_trip(points, split_name=split_name)
    if interpolate_seconds is not None:
        outbound = interpolate_points(outbound, interpolate_seconds)
        inbound = interpolate_points(inbound, interpolate_seconds)
    outbound_path = output_directory / "route_L1_to_L2.track.gpx"
    inbound_path = output_directory / "route_L2_to_L1.track.gpx"
    write_track(outbound_path, "L1 to L2", outbound)
    write_track(inbound_path, "L2 to L1", inbound)
    return outbound_path, inbound_path
