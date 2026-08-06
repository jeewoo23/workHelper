from __future__ import annotations

import json
from dataclasses import dataclass
from math import atan2, cos, isfinite, radians, sin, sqrt
from typing import Any, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .timing import DEFAULT_OSRM_URL


MAX_DIRECTIONS_RESPONSE_BYTES = 8 * 1024 * 1024
MIN_ROUTE_DISTANCE_METERS = 5.0


class DirectionsError(ValueError):
    """Base error for deterministic route generation."""


class DirectionsValidationError(DirectionsError):
    """Raised when a directions request is invalid."""


class DirectionsProviderError(DirectionsError):
    """Raised when a routing provider cannot return a safe route."""


@dataclass(frozen=True)
class DirectionsCoordinate:
    latitude: float
    longitude: float


@dataclass(frozen=True)
class GeneratedDirections:
    provider: str
    points: tuple[DirectionsCoordinate, ...]
    segment_durations_seconds: tuple[float, ...]
    distance_meters: float
    estimated_duration_seconds: float


class DirectionsProvider(Protocol):
    name: str

    def route(
        self,
        origin: DirectionsCoordinate,
        destination: DirectionsCoordinate,
    ) -> GeneratedDirections:
        ...


def generate_route(
    origin: DirectionsCoordinate,
    destination: DirectionsCoordinate,
    *,
    provider: DirectionsProvider,
) -> GeneratedDirections:
    """Return one validated road-following route for two coordinates."""
    _validate_coordinate(origin, "origin")
    _validate_coordinate(destination, "destination")
    if _distance_meters(origin, destination) < MIN_ROUTE_DISTANCE_METERS:
        raise DirectionsValidationError(
            "Origin and destination must be at least 5 meters apart"
        )

    generated = provider.route(origin, destination)
    _validate_generated_route(generated)
    return generated


class OsrmDirectionsProvider:
    """Generate full road geometry and edge timing from OSRM."""

    name = "OSRM"

    def __init__(
        self,
        base_url: str = DEFAULT_OSRM_URL,
        *,
        timeout_seconds: float = 15,
        transport: Any = urlopen,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._transport = transport

    def route(
        self,
        origin: DirectionsCoordinate,
        destination: DirectionsCoordinate,
    ) -> GeneratedDirections:
        coordinates = (
            f"{origin.longitude:.7f},{origin.latitude:.7f};"
            f"{destination.longitude:.7f},{destination.latitude:.7f}"
        )
        query = urlencode(
            {
                "overview": "full",
                "geometries": "geojson",
                "steps": "false",
                "annotations": "duration,distance",
                "alternatives": "false",
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
                raw_payload = response.read(MAX_DIRECTIONS_RESPONSE_BYTES + 1)
        except HTTPError as error:
            raise DirectionsProviderError(
                f"OSRM returned HTTP {error.code}"
            ) from error
        except (URLError, TimeoutError) as error:
            reason = getattr(error, "reason", error)
            raise DirectionsProviderError(
                f"OSRM could not be reached: {reason}"
            ) from error

        if len(raw_payload) > MAX_DIRECTIONS_RESPONSE_BYTES:
            raise DirectionsProviderError(
                "OSRM directions response was unexpectedly large"
            )
        try:
            payload = json.loads(raw_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DirectionsProviderError("OSRM returned invalid JSON") from error

        code = payload.get("code")
        routes = payload.get("routes")
        if code != "Ok" or not isinstance(routes, list) or not routes:
            message = payload.get("message") or code or "no route"
            raise DirectionsProviderError(
                f"OSRM could not find a driving route: {message}"
            )
        return self._generated_route(routes[0])

    def _generated_route(self, route: Any) -> GeneratedDirections:
        if not isinstance(route, dict):
            raise DirectionsProviderError("OSRM returned an invalid route")
        geometry = route.get("geometry")
        raw_coordinates = (
            geometry.get("coordinates")
            if isinstance(geometry, dict)
            else None
        )
        if not isinstance(raw_coordinates, list) or len(raw_coordinates) < 2:
            raise DirectionsProviderError(
                "OSRM returned incomplete road geometry"
            )

        points: list[DirectionsCoordinate] = []
        for raw_coordinate in raw_coordinates:
            if (
                not isinstance(raw_coordinate, list)
                or len(raw_coordinate) < 2
            ):
                raise DirectionsProviderError(
                    "OSRM returned an invalid road coordinate"
                )
            longitude, latitude = raw_coordinate[:2]
            if (
                isinstance(latitude, bool)
                or isinstance(longitude, bool)
                or not isinstance(latitude, (int, float))
                or not isinstance(longitude, (int, float))
            ):
                raise DirectionsProviderError(
                    "OSRM returned a non-numeric road coordinate"
                )
            point = DirectionsCoordinate(
                latitude=float(latitude),
                longitude=float(longitude),
            )
            try:
                _validate_coordinate(point, "provider route")
            except DirectionsValidationError as error:
                raise DirectionsProviderError(str(error)) from error
            points.append(point)

        distance = _finite_positive_number(route.get("distance"), "distance")
        duration = _finite_positive_number(route.get("duration"), "duration")
        segment_durations = self._segment_durations(
            route,
            points,
            duration,
        )
        return GeneratedDirections(
            provider=self.name,
            points=tuple(points),
            segment_durations_seconds=tuple(segment_durations),
            distance_meters=distance,
            estimated_duration_seconds=duration,
        )

    @staticmethod
    def _segment_durations(
        route: dict[str, Any],
        points: Sequence[DirectionsCoordinate],
        route_duration: float,
    ) -> list[float]:
        durations: list[float] = []
        legs = route.get("legs")
        if isinstance(legs, list):
            for leg in legs:
                annotation = (
                    leg.get("annotation")
                    if isinstance(leg, dict)
                    else None
                )
                raw_durations = (
                    annotation.get("duration")
                    if isinstance(annotation, dict)
                    else None
                )
                if isinstance(raw_durations, list):
                    for value in raw_durations:
                        if (
                            isinstance(value, bool)
                            or not isinstance(value, (int, float))
                            or not isfinite(float(value))
                            or float(value) < 0
                        ):
                            durations = []
                            break
                        durations.append(float(value))
                if not durations and raw_durations:
                    break

        if len(durations) != len(points) - 1 or sum(durations) <= 0:
            durations = [
                _distance_meters(first, second)
                for first, second in zip(points, points[1:])
            ]
        total = sum(durations)
        if total <= 0:
            raise DirectionsProviderError(
                "OSRM returned road geometry with no measurable length"
            )
        scale = route_duration / total
        scaled = [value * scale for value in durations]
        scaled[-1] += route_duration - sum(scaled)
        return scaled


def _validate_coordinate(
    coordinate: DirectionsCoordinate,
    label: str,
) -> None:
    if (
        isinstance(coordinate.latitude, bool)
        or not isinstance(coordinate.latitude, (int, float))
        or not isfinite(float(coordinate.latitude))
        or not -90 <= float(coordinate.latitude) <= 90
    ):
        raise DirectionsValidationError(
            f"{label.capitalize()} latitude must be between -90 and 90"
        )
    if (
        isinstance(coordinate.longitude, bool)
        or not isinstance(coordinate.longitude, (int, float))
        or not isfinite(float(coordinate.longitude))
        or not -180 <= float(coordinate.longitude) <= 180
    ):
        raise DirectionsValidationError(
            f"{label.capitalize()} longitude must be between -180 and 180"
        )


def _validate_generated_route(generated: GeneratedDirections) -> None:
    if len(generated.points) < 2:
        raise DirectionsProviderError(
            "The routing provider returned fewer than two points"
        )
    for point in generated.points:
        try:
            _validate_coordinate(point, "provider route")
        except DirectionsValidationError as error:
            raise DirectionsProviderError(str(error)) from error
    if len(generated.segment_durations_seconds) != len(generated.points) - 1:
        raise DirectionsProviderError(
            "The routing provider timing does not match its geometry"
        )
    if any(
        not isfinite(value) or value < 0
        for value in generated.segment_durations_seconds
    ):
        raise DirectionsProviderError(
            "The routing provider returned invalid segment timing"
        )
    if (
        not isfinite(generated.distance_meters)
        or generated.distance_meters <= 0
    ):
        raise DirectionsProviderError(
            "The routing provider returned an invalid distance"
        )
    if (
        not isfinite(generated.estimated_duration_seconds)
        or generated.estimated_duration_seconds <= 0
        or sum(generated.segment_durations_seconds) <= 0
    ):
        raise DirectionsProviderError(
            "The routing provider returned an invalid duration"
        )


def _finite_positive_number(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(float(value))
        or float(value) <= 0
    ):
        raise DirectionsProviderError(
            f"OSRM returned an invalid route {label}"
        )
    return float(value)


def _distance_meters(
    first: DirectionsCoordinate,
    second: DirectionsCoordinate,
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
