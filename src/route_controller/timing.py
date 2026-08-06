from __future__ import annotations

import json
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime
from math import atan2, cos, isfinite, radians, sin, sqrt
from typing import Any, Optional, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_OSRM_URL = "https://router.project-osrm.org"
MAX_OSRM_RESPONSE_BYTES = 2 * 1024 * 1024


class TimingProfileError(ValueError):
    """Raised when a route cannot receive a valid playback timing profile."""


class RoadTimingProvider(Protocol):
    name: str

    def estimate(self, points: Sequence["TimingCoordinate"]) -> "RoadTimingEstimate":
        ...


@dataclass(frozen=True)
class TimingCoordinate:
    latitude: float
    longitude: float


@dataclass(frozen=True)
class RoadTimingEstimate:
    provider: str
    segment_durations_seconds: tuple[float, ...]
    estimated_duration_seconds: float
    anchor_count: int


@dataclass(frozen=True)
class TimingPlan:
    mode: str
    offsets_seconds: tuple[float, ...]
    estimated_duration_seconds: float
    provider: str = ""
    warning: str = ""

    @property
    def duration_seconds(self) -> float:
        return self.offsets_seconds[-1]


def build_timing_plan(
    points: Sequence[TimingCoordinate],
    *,
    requested_duration_seconds: Optional[float],
    source_times: Optional[Sequence[Optional[datetime]]] = None,
    mode: str = "auto",
    provider: Optional[RoadTimingProvider] = None,
) -> TimingPlan:
    """Build one complete, monotonic timestamp plan for a route."""
    if len(points) < 2:
        raise TimingProfileError("A timing profile requires at least two points")
    if requested_duration_seconds is not None and requested_duration_seconds <= 0:
        raise TimingProfileError("Playback duration must be positive")
    if mode not in {"auto", "source", "route-aware", "uniform"}:
        raise TimingProfileError(f"Unsupported timing mode: {mode}")

    source_offsets = _source_offsets(source_times, len(points))
    if mode == "source" or (mode == "auto" and source_offsets is not None):
        if source_offsets is None:
            raise TimingProfileError(
                "Source timing requires a timestamp on every GPX point"
            )
        original_duration = source_offsets[-1]
        duration = requested_duration_seconds or original_duration
        return TimingPlan(
            mode="source",
            offsets_seconds=_scale_offsets(source_offsets, duration),
            estimated_duration_seconds=original_duration,
        )

    if mode in {"auto", "route-aware"} and provider is not None:
        try:
            estimate = provider.estimate(points)
            _validate_estimate(estimate, len(points))
            duration = (
                requested_duration_seconds
                if requested_duration_seconds is not None
                else estimate.estimated_duration_seconds
            )
            return TimingPlan(
                mode="route-aware",
                offsets_seconds=_offsets_from_segments(
                    estimate.segment_durations_seconds,
                    duration,
                ),
                estimated_duration_seconds=estimate.estimated_duration_seconds,
                provider=estimate.provider,
            )
        except (TimingProfileError, OSError) as error:
            if mode == "route-aware" or requested_duration_seconds is None:
                raise TimingProfileError(
                    f"Road-aware timing is unavailable: {error}"
                ) from error
            return _uniform_plan(
                points,
                requested_duration_seconds,
                warning=(
                    f"Road-aware timing was unavailable ({error}); "
                    "uniform timing was used."
                ),
            )

    if mode == "route-aware":
        raise TimingProfileError("Road-aware timing has no configured provider")
    if requested_duration_seconds is None:
        raise TimingProfileError(
            "A playback duration is required when road-aware timing is unavailable"
        )
    warning = (
        "No road timing provider is configured; uniform timing was used."
        if mode == "auto"
        else ""
    )
    return _uniform_plan(points, requested_duration_seconds, warning=warning)


class OsrmRouteTimingProvider:
    """Estimate relative road speeds by routing through sampled GPX anchors."""

    name = "OSRM"

    def __init__(
        self,
        base_url: str = DEFAULT_OSRM_URL,
        *,
        timeout_seconds: float = 12,
        max_waypoints: int = 90,
        transport: Any = urlopen,
    ) -> None:
        if max_waypoints < 2:
            raise ValueError("OSRM timing requires at least two waypoints")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_waypoints = max_waypoints
        self._transport = transport

    def estimate(
        self, points: Sequence[TimingCoordinate]
    ) -> RoadTimingEstimate:
        if len(points) < 2:
            raise TimingProfileError("OSRM timing requires at least two points")

        anchor_indices = _distance_anchor_indices(points, self.max_waypoints)
        coordinates = ";".join(
            f"{points[index].longitude:.7f},{points[index].latitude:.7f}"
            for index in anchor_indices
        )
        query = urlencode(
            {
                "overview": "false",
                "steps": "false",
                "annotations": "false",
                "continue_straight": "true",
            }
        )
        request = Request(
            f"{self.base_url}/route/v1/driving/{coordinates}?{query}",
            headers={
                "Accept": "application/json",
                "User-Agent": "CentralBlue-RouteController/0.1",
            },
        )

        try:
            with self._transport(
                request, timeout=self.timeout_seconds
            ) as response:
                raw_payload = response.read(MAX_OSRM_RESPONSE_BYTES + 1)
        except HTTPError as error:
            raise TimingProfileError(
                f"OSRM returned HTTP {error.code}"
            ) from error
        except (URLError, TimeoutError) as error:
            reason = getattr(error, "reason", error)
            raise TimingProfileError(f"OSRM could not be reached: {reason}") from error

        if len(raw_payload) > MAX_OSRM_RESPONSE_BYTES:
            raise TimingProfileError("OSRM response was unexpectedly large")
        try:
            payload = json.loads(raw_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TimingProfileError("OSRM returned invalid JSON") from error

        code = payload.get("code")
        routes = payload.get("routes")
        if code != "Ok" or not isinstance(routes, list) or not routes:
            message = payload.get("message") or code or "no route"
            raise TimingProfileError(f"OSRM could not match the route: {message}")
        legs = routes[0].get("legs")
        if not isinstance(legs, list) or len(legs) != len(anchor_indices) - 1:
            raise TimingProfileError("OSRM returned an incomplete timing profile")

        leg_durations: list[float] = []
        for leg in legs:
            duration = leg.get("duration") if isinstance(leg, dict) else None
            if (
                isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not isfinite(float(duration))
                or float(duration) < 0
            ):
                raise TimingProfileError("OSRM returned an invalid leg duration")
            leg_durations.append(float(duration))

        segment_distances = [
            _distance_meters(first, second)
            for first, second in zip(points, points[1:])
        ]
        segment_durations = [0.0] * (len(points) - 1)
        for leg_index, leg_duration in enumerate(leg_durations):
            start = anchor_indices[leg_index]
            end = anchor_indices[leg_index + 1]
            weights = segment_distances[start:end]
            total_weight = sum(weights)
            if total_weight <= 0:
                weights = [1.0] * max(1, end - start)
                total_weight = sum(weights)
            for offset, weight in enumerate(weights):
                segment_durations[start + offset] = (
                    leg_duration * weight / total_weight
                )

        estimated_duration = sum(segment_durations)
        if estimated_duration <= 0:
            raise TimingProfileError("OSRM estimated a zero-duration route")
        return RoadTimingEstimate(
            provider=self.name,
            segment_durations_seconds=tuple(segment_durations),
            estimated_duration_seconds=estimated_duration,
            anchor_count=len(anchor_indices),
        )


def _source_offsets(
    source_times: Optional[Sequence[Optional[datetime]]],
    point_count: int,
) -> Optional[tuple[float, ...]]:
    if source_times is None or len(source_times) != point_count:
        return None
    if any(value is None for value in source_times):
        return None
    times = [value for value in source_times if value is not None]
    started_at = times[0]
    offsets = tuple((value - started_at).total_seconds() for value in times)
    if offsets[-1] <= 0:
        return None
    if any(second < first for first, second in zip(offsets, offsets[1:])):
        return None
    return offsets


def _validate_estimate(estimate: RoadTimingEstimate, point_count: int) -> None:
    if len(estimate.segment_durations_seconds) != point_count - 1:
        raise TimingProfileError("Road timing profile does not match the GPX geometry")
    if (
        not isfinite(estimate.estimated_duration_seconds)
        or estimate.estimated_duration_seconds <= 0
    ):
        raise TimingProfileError("Road timing profile has an invalid duration")
    if any(
        not isfinite(duration) or duration < 0
        for duration in estimate.segment_durations_seconds
    ):
        raise TimingProfileError("Road timing profile contains invalid segments")
    if sum(estimate.segment_durations_seconds) <= 0:
        raise TimingProfileError("Road timing profile has no positive segments")


def _uniform_plan(
    points: Sequence[TimingCoordinate],
    duration_seconds: float,
    *,
    warning: str,
) -> TimingPlan:
    segment_distances = [
        _distance_meters(first, second)
        for first, second in zip(points, points[1:])
    ]
    if sum(segment_distances) <= 0:
        segment_distances = [1.0] * (len(points) - 1)
    return TimingPlan(
        mode="uniform",
        offsets_seconds=_offsets_from_segments(segment_distances, duration_seconds),
        estimated_duration_seconds=duration_seconds,
        warning=warning,
    )


def _offsets_from_segments(
    segment_values: Sequence[float],
    duration_seconds: float,
) -> tuple[float, ...]:
    total = sum(segment_values)
    if total <= 0:
        raise TimingProfileError("Timing segments must have a positive total")
    scale = duration_seconds / total
    offsets = [0.0]
    for value in segment_values:
        offsets.append(offsets[-1] + value * scale)
    offsets[-1] = duration_seconds
    return tuple(offsets)


def _scale_offsets(
    offsets: Sequence[float],
    duration_seconds: float,
) -> tuple[float, ...]:
    original_duration = offsets[-1]
    if original_duration <= 0:
        raise TimingProfileError("Source timestamps have no positive duration")
    scale = duration_seconds / original_duration
    scaled = tuple(value * scale for value in offsets)
    return scaled[:-1] + (duration_seconds,)


def _distance_anchor_indices(
    points: Sequence[TimingCoordinate],
    maximum_count: int,
) -> list[int]:
    if len(points) <= maximum_count:
        return list(range(len(points)))

    cumulative = [0.0]
    for first, second in zip(points, points[1:]):
        cumulative.append(cumulative[-1] + _distance_meters(first, second))
    total_distance = cumulative[-1]
    if total_distance <= 0:
        step = (len(points) - 1) / (maximum_count - 1)
        indices = [round(index * step) for index in range(maximum_count)]
        indices[-1] = len(points) - 1
        return sorted(set(indices))

    indices = [0]
    for anchor_number in range(1, maximum_count - 1):
        target = total_distance * anchor_number / (maximum_count - 1)
        candidate = bisect_left(cumulative, target)
        if candidate >= len(points):
            candidate = len(points) - 1
        if candidate > 0 and (
            target - cumulative[candidate - 1]
            < cumulative[candidate] - target
        ):
            candidate -= 1
        if candidate > indices[-1]:
            indices.append(candidate)
    if indices[-1] != len(points) - 1:
        indices.append(len(points) - 1)
    return indices


def _distance_meters(
    first: TimingCoordinate, second: TimingCoordinate
) -> float:
    first_latitude = radians(first.latitude)
    second_latitude = radians(second.latitude)
    latitude_delta = second_latitude - first_latitude
    longitude_delta = radians(second.longitude - first.longitude)
    haversine = (
        sin(latitude_delta / 2) ** 2
        + cos(first_latitude)
        * cos(second_latitude)
        * sin(longitude_delta / 2) ** 2
    )
    clamped = min(1.0, max(0.0, haversine))
    return 6371000 * 2 * atan2(sqrt(clamped), sqrt(1 - clamped))
