from __future__ import annotations

from io import BytesIO
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence
from xml.etree import ElementTree as ET

from .timing import (
    RoadTimingProvider,
    TimingCoordinate,
    TimingPlan,
    TimingProfileError,
    build_timing_plan,
)

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


@dataclass(frozen=True)
class GpxGeometrySummary:
    name: str
    geometry_type: str
    point_count: int
    timestamped_point_count: int
    segment_count: int
    start: tuple[float, float]
    end: tuple[float, float]
    bounds: tuple[float, float, float, float]
    start_name: str = ""
    end_name: str = ""
    duration_seconds: Optional[float] = None


@dataclass(frozen=True)
class GpxSourcePoint:
    latitude: float
    longitude: float
    time: Optional[datetime] = None
    name: Optional[str] = None


@dataclass(frozen=True)
class ParsedGpxGeometry:
    name: str
    geometry_type: str
    segment_count: int
    points: tuple[GpxSourcePoint, ...]


@dataclass(frozen=True)
class PreparedGpxPlayback:
    name: str
    points: tuple[RoutePoint, ...]
    preview_points: tuple[RoutePoint, ...]
    timing: TimingPlan


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


def _optional_text(element: ET.Element, name: str) -> Optional[str]:
    child = _first_child(element, name)
    if child is None or child.text is None or not child.text.strip():
        return None
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


def inspect_gpx_content(
    content: str, *, fallback_name: str = "Imported route"
) -> GpxGeometrySummary:
    geometry = parse_gpx_geometry_content(content, fallback_name=fallback_name)
    timestamped_point_count = sum(
        point.time is not None for point in geometry.points
    )
    coordinates = [
        (point.latitude, point.longitude)
        for point in geometry.points
    ]
    latitudes = [latitude for latitude, _ in coordinates]
    longitudes = [longitude for _, longitude in coordinates]
    return GpxGeometrySummary(
        name=geometry.name,
        geometry_type=geometry.geometry_type,
        point_count=len(geometry.points),
        timestamped_point_count=timestamped_point_count,
        segment_count=geometry.segment_count,
        start=coordinates[0],
        end=coordinates[-1],
        bounds=(
            min(latitudes),
            min(longitudes),
            max(latitudes),
            max(longitudes),
        ),
        start_name=geometry.points[0].name or "",
        end_name=geometry.points[-1].name or "",
        duration_seconds=(
            (
                geometry.points[-1].time - geometry.points[0].time
            ).total_seconds()
            if (
                timestamped_point_count == len(geometry.points)
                and geometry.points[0].time is not None
                and geometry.points[-1].time is not None
                and geometry.points[-1].time > geometry.points[0].time
            )
            else None
        ),
    )


def parse_gpx_geometry_content(
    content: str, *, fallback_name: str = "Imported route"
) -> ParsedGpxGeometry:
    if not content.strip():
        raise GpxValidationError("GPX content is empty")
    lowered = content.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise GpxValidationError("GPX document type and entity declarations are not allowed")

    try:
        root = ET.fromstring(content.encode("utf-8"))
    except (ET.ParseError, ValueError) as error:
        raise GpxValidationError(f"GPX content is not valid XML: {error}") from error
    if _local_name(root.tag) != "gpx":
        raise GpxValidationError("XML root element must be 'gpx'")

    containers = _children(root, "trk")
    if containers:
        if len(containers) != 1:
            raise GpxValidationError(
                f"Expected exactly one GPX track, found {len(containers)}"
            )
        geometry_type = "track"
        track = containers[0]
        segments = _children(track, "trkseg")
        if not segments:
            raise GpxValidationError("GPX track contains no track segments")
        point_elements = [
            point
            for segment in segments
            for point in _children(segment, "trkpt")
        ]
        name = _optional_text(track, "name") or fallback_name
        segment_count = len(segments)
    else:
        containers = _children(root, "rte")
        if containers:
            if len(containers) != 1:
                raise GpxValidationError(
                    f"Expected exactly one GPX route, found {len(containers)}"
                )
            geometry_type = "route"
            route = containers[0]
            point_elements = _children(route, "rtept")
            name = _optional_text(route, "name") or fallback_name
            segment_count = 1
        else:
            geometry_type = "waypoints"
            point_elements = _children(root, "wpt")
            name = fallback_name
            segment_count = 1

    if len(point_elements) < 2:
        raise GpxValidationError("Imported route must contain at least two points")

    points: list[GpxSourcePoint] = []
    previous_time: Optional[datetime] = None
    for index, element in enumerate(point_elements):
        try:
            latitude = float(element.attrib["lat"])
            longitude = float(element.attrib["lon"])
        except (KeyError, ValueError) as error:
            raise GpxValidationError(
                f"GPX point {index} has invalid latitude/longitude"
            ) from error
        if not -90 <= latitude <= 90:
            raise GpxValidationError(
                f"Point {index} latitude is outside [-90, 90]: {latitude}"
            )
        if not -180 <= longitude <= 180:
            raise GpxValidationError(
                f"Point {index} longitude is outside [-180, 180]: {longitude}"
            )
        timestamp = None
        time_text = _optional_text(element, "time")
        if time_text is not None:
            timestamp = _parse_time(time_text)
            if previous_time is not None and timestamp < previous_time:
                raise GpxValidationError(
                    f"Point {index} timestamp precedes the previous timestamped point"
                )
            previous_time = timestamp
        points.append(
            GpxSourcePoint(
                latitude=latitude,
                longitude=longitude,
                time=timestamp,
                name=_optional_text(element, "name"),
            )
        )

    return ParsedGpxGeometry(
        name=name,
        geometry_type=geometry_type,
        segment_count=segment_count,
        points=tuple(points),
    )


def prepare_gpx_playback(
    content: str,
    *,
    fallback_name: str = "Imported route",
    duration_seconds: Optional[float] = None,
    interpolate_seconds: Optional[float] = 0.5,
    start_time: Optional[datetime] = None,
    timing_mode: str = "auto",
    timing_provider: Optional[RoadTimingProvider] = None,
) -> tuple[str, list[RoutePoint]]:
    prepared = prepare_gpx_playback_result(
        content,
        fallback_name=fallback_name,
        duration_seconds=duration_seconds,
        interpolate_seconds=interpolate_seconds,
        start_time=start_time,
        timing_mode=timing_mode,
        timing_provider=timing_provider,
    )
    return prepared.name, list(prepared.points)


def prepare_gpx_playback_result(
    content: str,
    *,
    fallback_name: str = "Imported route",
    duration_seconds: Optional[float] = None,
    interpolate_seconds: Optional[float] = 0.5,
    start_time: Optional[datetime] = None,
    timing_mode: str = "auto",
    timing_provider: Optional[RoadTimingProvider] = None,
) -> PreparedGpxPlayback:
    geometry = parse_gpx_geometry_content(content, fallback_name=fallback_name)
    if duration_seconds is not None and duration_seconds <= 0:
        raise GpxValidationError("Playback duration must be positive")
    if start_time is not None and start_time.tzinfo is None:
        raise GpxValidationError("Playback start time must include a timezone")

    source_points = list(geometry.points)
    try:
        timing = build_timing_plan(
            [
                TimingCoordinate(
                    latitude=point.latitude,
                    longitude=point.longitude,
                )
                for point in source_points
            ],
            requested_duration_seconds=duration_seconds,
            source_times=[point.time for point in source_points],
            mode=timing_mode,
            provider=timing_provider,
        )
    except TimingProfileError as error:
        raise GpxValidationError(str(error)) from error

    source_start = source_points[0].time
    playback_start = start_time or source_start or datetime.now(timezone.utc).replace(
        microsecond=0
    )
    timed_source_points = [
        RoutePoint(
            latitude=point.latitude,
            longitude=point.longitude,
            time=playback_start + timedelta(seconds=timing.offsets_seconds[index]),
            name=point.name,
        )
        for index, point in enumerate(source_points)
    ]

    validate_points(timed_source_points)
    points = timed_source_points
    if interpolate_seconds is not None:
        points = resample_points(points, interpolate_seconds)
    return PreparedGpxPlayback(
        name=geometry.name,
        points=tuple(points),
        preview_points=tuple(timed_source_points),
        timing=timing,
    )


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
    points: Sequence[RoutePoint], interval_seconds: float
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


def resample_points(
    points: Sequence[RoutePoint], interval_seconds: float
) -> list[RoutePoint]:
    """Sample a timed route at a fixed cadence and retain the exact endpoints."""
    validate_points(points)
    if len(points) < 2:
        raise GpxValidationError("Route resampling requires at least two points")
    if interval_seconds <= 0:
        raise GpxValidationError(
            f"Resampling interval must be positive: {interval_seconds}"
        )

    started_at = points[0].time
    ended_at = points[-1].time
    if ended_at <= started_at:
        raise GpxValidationError(
            "Route resampling requires a positive duration"
        )

    interval = timedelta(seconds=interval_seconds)
    sampled = [points[0]]
    source_index = 0
    cursor = started_at + interval
    while cursor < ended_at:
        while (
            source_index + 1 < len(points) - 1
            and points[source_index + 1].time <= cursor
        ):
            source_index += 1

        segment_start = points[source_index]
        segment_end = points[source_index + 1]
        segment_duration = (
            segment_end.time - segment_start.time
        ).total_seconds()
        if segment_duration <= 0:
            raise GpxValidationError(
                "Route contains duplicate timestamps that could not be resampled"
            )
        fraction = (
            (cursor - segment_start.time).total_seconds()
            / segment_duration
        )
        sampled.append(
            RoutePoint(
                latitude=segment_start.latitude
                + (
                    (segment_end.latitude - segment_start.latitude)
                    * fraction
                ),
                longitude=segment_start.longitude
                + (
                    (segment_end.longitude - segment_start.longitude)
                    * fraction
                ),
                time=cursor,
                name=(
                    segment_start.name
                    if cursor == segment_start.time
                    else None
                ),
            )
        )
        cursor += interval
    sampled.append(points[-1])

    if any(
        second.time <= first.time
        for first, second in zip(sampled, sampled[1:])
    ):
        raise GpxValidationError(
            "Resampled route timestamps must be strictly increasing"
        )
    validate_points(sampled)
    return sampled


def _format_time(value: datetime) -> str:
    if value.microsecond % 1000:
        timespec = "microseconds"
    elif value.microsecond:
        timespec = "milliseconds"
    else:
        timespec = "seconds"
    utc_text = value.isoformat(timespec=timespec)
    return utc_text.replace("+00:00", "Z")


def track_xml(name: str, points: Sequence[RoutePoint]) -> str:
    validate_points(points)
    root = ET.Element(
        f"{{{GPX_NAMESPACE}}}gpx",
        {"version": "1.1", "creator": "Central Blue Route Controller"},
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
    buffer = BytesIO()
    ET.ElementTree(root).write(
        buffer,
        encoding="utf-8",
        xml_declaration=True,
    )
    return buffer.getvalue().decode("utf-8")


def write_track(path: Path, name: str, points: Sequence[RoutePoint]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(track_xml(name, points), encoding="utf-8")


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
    interpolate_seconds: Optional[float] = None,
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
