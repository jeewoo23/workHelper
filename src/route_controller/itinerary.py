from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .directions import (
    DirectionsCoordinate,
    DirectionsProvider,
    DirectionsProviderError,
    DirectionsValidationError,
    generate_route,
)
from .gpx import RoutePoint


MAX_DESCRIPTION_LENGTH = 10_000
MAX_ITINERARY_POINTS = 12_000
MAX_PROVIDER_RESPONSE_BYTES = 2 * 1024 * 1024
PLACE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
LOCAL_TIME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$")


class ItineraryError(ValueError):
    """Base error for natural-language itinerary planning."""


class ItineraryValidationError(ItineraryError):
    """Raised when an itinerary is unsafe or internally inconsistent."""


class ItineraryProviderError(ItineraryError):
    """Raised when the language model cannot return a valid itinerary."""


class GeocodingError(ItineraryError):
    """Raised when a place cannot be resolved to a coordinate."""


@dataclass(frozen=True)
class ItineraryPlace:
    id: str
    label: str
    query: str


@dataclass(frozen=True)
class ItinerarySegment:
    kind: str
    start: datetime
    end: datetime
    place_id: str | None
    origin_id: str | None
    destination_id: str | None
    mode: str


@dataclass(frozen=True)
class HourlyItinerary:
    name: str
    day: date
    timezone_name: str
    places: tuple[ItineraryPlace, ...]
    segments: tuple[ItinerarySegment, ...]
    assumptions: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedPlace:
    id: str
    label: str
    query: str
    latitude: float
    longitude: float
    display_name: str


@dataclass(frozen=True)
class ComposedItinerary:
    points: tuple[RoutePoint, ...]
    distance_meters: float
    travel_seconds: float
    directions_providers: tuple[str, ...]


class ItineraryPlanner(Protocol):
    model: str

    def interpret(
        self, description: str, *, day: str, timezone_name: str
    ) -> HourlyItinerary:
        ...


class PlaceGeocoder(Protocol):
    name: str

    def geocode(self, place: ItineraryPlace) -> ResolvedPlace:
        ...


ITINERARY_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "name",
        "date",
        "timezone",
        "places",
        "segments",
        "assumptions",
    ],
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 120},
        "date": {"type": "string", "format": "date"},
        "timezone": {"type": "string", "minLength": 1, "maxLength": 80},
        "places": {
            "type": "array",
            "minItems": 1,
            "maxItems": 24,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "label", "query"],
                "properties": {
                    "id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{0,39}$"},
                    "label": {"type": "string", "minLength": 1, "maxLength": 100},
                    "query": {"type": "string", "minLength": 1, "maxLength": 300},
                },
            },
        },
        "segments": {
            "type": "array",
            "minItems": 1,
            "maxItems": 48,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "kind",
                    "start",
                    "end",
                    "placeId",
                    "originId",
                    "destinationId",
                    "mode",
                ],
                "properties": {
                    "kind": {"type": "string", "enum": ["stay", "travel"]},
                    "start": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}(?::\\d{2})?$"},
                    "end": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}(?::\\d{2})?$"},
                    "placeId": {"type": ["string", "null"]},
                    "originId": {"type": ["string", "null"]},
                    "destinationId": {"type": ["string", "null"]},
                    "mode": {"type": "string", "enum": ["driving"]},
                },
            },
        },
        "assumptions": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string", "maxLength": 300},
        },
    },
}


def validate_planner_request(
    description: Any, day: Any, timezone_name: Any
) -> tuple[str, str, str]:
    if not isinstance(description, str) or not description.strip():
        raise ItineraryValidationError("Describe the day you want to simulate")
    description = description.strip()
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise ItineraryValidationError("The itinerary description is too long")
    parsed_day = _parse_day(day)
    zone_name = _parse_timezone(timezone_name).key
    return description, parsed_day.isoformat(), zone_name


def parse_itinerary(payload: Any) -> HourlyItinerary:
    if not isinstance(payload, dict):
        raise ItineraryValidationError("The itinerary must be a JSON object")
    expected = {"name", "date", "timezone", "places", "segments", "assumptions"}
    if set(payload) != expected:
        raise ItineraryValidationError("The itinerary has missing or unexpected fields")

    name = _string(payload["name"], "name", 120)
    day = _parse_day(payload["date"])
    zone = _parse_timezone(payload["timezone"])
    raw_places = payload["places"]
    if not isinstance(raw_places, list) or not 1 <= len(raw_places) <= 24:
        raise ItineraryValidationError("The itinerary must contain 1 to 24 places")
    places: list[ItineraryPlace] = []
    ids: set[str] = set()
    for index, raw in enumerate(raw_places):
        if not isinstance(raw, dict) or set(raw) != {"id", "label", "query"}:
            raise ItineraryValidationError(f"Place {index + 1} is invalid")
        place_id = _string(raw["id"], f"place {index + 1} id", 40)
        if not PLACE_ID_PATTERN.fullmatch(place_id):
            raise ItineraryValidationError(f"Place {index + 1} has an invalid id")
        if place_id in ids:
            raise ItineraryValidationError(f"Duplicate place id: {place_id}")
        ids.add(place_id)
        places.append(
            ItineraryPlace(
                id=place_id,
                label=_string(raw["label"], f"place {index + 1} label", 100),
                query=_string(raw["query"], f"place {index + 1} query", 300),
            )
        )

    raw_segments = payload["segments"]
    if not isinstance(raw_segments, list) or not 1 <= len(raw_segments) <= 48:
        raise ItineraryValidationError("The itinerary must contain 1 to 48 segments")
    segments: list[ItinerarySegment] = []
    segment_fields = {
        "kind", "start", "end", "placeId", "originId", "destinationId", "mode"
    }
    for index, raw in enumerate(raw_segments):
        if not isinstance(raw, dict) or set(raw) != segment_fields:
            raise ItineraryValidationError(f"Segment {index + 1} is invalid")
        kind = raw["kind"]
        if kind not in {"stay", "travel"} or raw["mode"] != "driving":
            raise ItineraryValidationError(
                f"Segment {index + 1} must be a stay or driving travel segment"
            )
        start = _parse_local_time(raw["start"], zone, f"segment {index + 1} start")
        end = _parse_local_time(raw["end"], zone, f"segment {index + 1} end")
        if end <= start:
            raise ItineraryValidationError(f"Segment {index + 1} must end after it starts")
        place_id = _nullable_id(raw["placeId"], ids, f"segment {index + 1} placeId")
        origin_id = _nullable_id(raw["originId"], ids, f"segment {index + 1} originId")
        destination_id = _nullable_id(
            raw["destinationId"], ids, f"segment {index + 1} destinationId"
        )
        if kind == "stay":
            if place_id is None or origin_id is not None or destination_id is not None:
                raise ItineraryValidationError(
                    f"Stay segment {index + 1} needs only placeId"
                )
        elif (
            place_id is not None
            or origin_id is None
            or destination_id is None
            or origin_id == destination_id
        ):
            raise ItineraryValidationError(
                f"Travel segment {index + 1} needs distinct originId and destinationId"
            )
        segments.append(
            ItinerarySegment(
                kind=kind,
                start=start,
                end=end,
                place_id=place_id,
                origin_id=origin_id,
                destination_id=destination_id,
                mode="driving",
            )
        )

    for index, (previous, current) in enumerate(zip(segments, segments[1:]), 2):
        if current.start != previous.end:
            raise ItineraryValidationError(
                f"Segment {index} must start exactly when the previous segment ends"
            )
        if _segment_destination(previous) != _segment_origin(current):
            raise ItineraryValidationError(
                f"Segment {index} does not continue from the previous location"
            )
    total_seconds = (segments[-1].end - segments[0].start).total_seconds()
    if total_seconds > 86_400:
        raise ItineraryValidationError("An itinerary cannot span more than 24 hours")
    if segments[0].start.date() != day:
        raise ItineraryValidationError("The itinerary must start on its requested date")

    raw_assumptions = payload["assumptions"]
    if not isinstance(raw_assumptions, list) or len(raw_assumptions) > 20:
        raise ItineraryValidationError("The itinerary assumptions are invalid")
    assumptions = tuple(
        _string(value, f"assumption {index + 1}", 300)
        for index, value in enumerate(raw_assumptions)
    )
    return HourlyItinerary(
        name=name,
        day=day,
        timezone_name=zone.key,
        places=tuple(places),
        segments=tuple(segments),
        assumptions=assumptions,
    )


def itinerary_payload(itinerary: HourlyItinerary) -> dict[str, Any]:
    return {
        "name": itinerary.name,
        "date": itinerary.day.isoformat(),
        "timezone": itinerary.timezone_name,
        "places": [
            {"id": place.id, "label": place.label, "query": place.query}
            for place in itinerary.places
        ],
        "segments": [
            {
                "kind": segment.kind,
                "start": segment.start.replace(tzinfo=None).isoformat(timespec="seconds"),
                "end": segment.end.replace(tzinfo=None).isoformat(timespec="seconds"),
                "placeId": segment.place_id,
                "originId": segment.origin_id,
                "destinationId": segment.destination_id,
                "mode": segment.mode,
            }
            for segment in itinerary.segments
        ],
        "assumptions": list(itinerary.assumptions),
    }


def resolved_place_payload(place: ResolvedPlace) -> dict[str, Any]:
    return {
        "id": place.id,
        "label": place.label,
        "query": place.query,
        "latitude": place.latitude,
        "longitude": place.longitude,
        "displayName": place.display_name,
    }


def parse_resolved_places(payload: Any, itinerary: HourlyItinerary) -> tuple[ResolvedPlace, ...]:
    if not isinstance(payload, list) or len(payload) != len(itinerary.places):
        raise ItineraryValidationError("Confirm one resolved coordinate for every place")
    expected = {place.id: place for place in itinerary.places}
    resolved: dict[str, ResolvedPlace] = {}
    required = {"id", "label", "query", "latitude", "longitude", "displayName"}
    for index, raw in enumerate(payload):
        if not isinstance(raw, dict) or set(raw) != required:
            raise ItineraryValidationError(f"Resolved place {index + 1} is invalid")
        place_id = raw["id"]
        if place_id not in expected or place_id in resolved:
            raise ItineraryValidationError("Resolved place identifiers do not match")
        source = expected[place_id]
        latitude = _coordinate(raw["latitude"], -90, 90, "latitude")
        longitude = _coordinate(raw["longitude"], -180, 180, "longitude")
        resolved[place_id] = ResolvedPlace(
            id=place_id,
            label=source.label,
            query=source.query,
            latitude=latitude,
            longitude=longitude,
            display_name=_string(raw["displayName"], "resolved display name", 500),
        )
    return tuple(resolved[place.id] for place in itinerary.places)


def compose_itinerary(
    itinerary: HourlyItinerary,
    resolved_places: tuple[ResolvedPlace, ...],
    *,
    directions_provider: DirectionsProvider,
    heartbeat_seconds: int = 3_600,
    max_points: int = MAX_ITINERARY_POINTS,
) -> ComposedItinerary:
    if heartbeat_seconds < 60:
        raise ItineraryValidationError("Stationary heartbeat must be at least 60 seconds")
    places = {place.id: place for place in resolved_places}
    if set(places) != {place.id for place in itinerary.places}:
        raise ItineraryValidationError("Resolved places do not match the itinerary")
    points: list[RoutePoint] = []
    distance_meters = 0.0
    travel_seconds = 0.0
    providers: list[str] = []

    def append_point(point: RoutePoint) -> None:
        if points and point.time < points[-1].time:
            raise ItineraryValidationError("Generated route timestamps are out of order")
        if points and point.time == points[-1].time:
            if (
                abs(point.latitude - points[-1].latitude) < 1e-9
                and abs(point.longitude - points[-1].longitude) < 1e-9
            ):
                if point.name and not points[-1].name:
                    points[-1] = point
                return
            points[-1] = point
            return
        points.append(point)
        if len(points) > max_points:
            raise ItineraryValidationError(
                f"The generated itinerary exceeds the {max_points:,}-point safety limit"
            )

    for segment in itinerary.segments:
        if segment.kind == "stay":
            place = places[segment.place_id or ""]
            cursor = segment.start
            append_point(_route_point(place, cursor, place.label))
            cursor += timedelta(seconds=heartbeat_seconds)
            while cursor < segment.end:
                append_point(_route_point(place, cursor, place.label))
                cursor += timedelta(seconds=heartbeat_seconds)
            append_point(_route_point(place, segment.end, place.label))
            continue

        origin = places[segment.origin_id or ""]
        destination = places[segment.destination_id or ""]
        try:
            generated = generate_route(
                DirectionsCoordinate(origin.latitude, origin.longitude),
                DirectionsCoordinate(destination.latitude, destination.longitude),
                provider=directions_provider,
            )
        except (DirectionsValidationError, DirectionsProviderError) as error:
            raise ItineraryProviderError(str(error)) from error
        window_seconds = (segment.end - segment.start).total_seconds()
        source_seconds = sum(generated.segment_durations_seconds)
        if source_seconds <= 0:
            raise ItineraryProviderError("The directions provider returned invalid timing")
        offsets = [0.0]
        for duration in generated.segment_durations_seconds:
            offsets.append(offsets[-1] + duration * window_seconds / source_seconds)
        offsets[-1] = window_seconds
        for index, coordinate in enumerate(generated.points):
            append_point(
                RoutePoint(
                    latitude=coordinate.latitude,
                    longitude=coordinate.longitude,
                    time=(segment.start + timedelta(seconds=offsets[index])).astimezone(
                        timezone.utc
                    ),
                    name=(
                        origin.label
                        if index == 0
                        else destination.label
                        if index == len(generated.points) - 1
                        else None
                    ),
                )
            )
        distance_meters += generated.distance_meters
        travel_seconds += window_seconds
        if generated.provider not in providers:
            providers.append(generated.provider)

    if len(points) < 2 or points[-1].time <= points[0].time:
        raise ItineraryValidationError("The itinerary needs a positive playback duration")
    return ComposedItinerary(
        points=tuple(points),
        distance_meters=distance_meters,
        travel_seconds=travel_seconds,
        directions_providers=tuple(providers),
    )


class OpenAIItineraryPlanner:
    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gpt-5.4-mini",
        endpoint: str = "https://api.openai.com/v1/responses",
        timeout_seconds: float = 45,
        transport: Any = urlopen,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self._transport = transport

    def interpret(
        self, description: str, *, day: str, timezone_name: str
    ) -> HourlyItinerary:
        description, day, timezone_name = validate_planner_request(
            description, day, timezone_name
        )
        request_payload = {
            "model": self.model,
            "input": [
                {
                    "role": "system",
                    "content": (
                        "Convert a user's day into one continuous itinerary. "
                        "Use only stay and driving travel segments, with no gaps or overlaps. "
                        "Use local wall-clock timestamps without UTC offsets. Create reusable place "
                        "records with specific geocoding queries, but never invent coordinates. "
                        "If a detail is missing, make a conservative assumption and list it."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Requested date: {day}\nTimezone: {timezone_name}\n"
                        f"Description:\n{description}"
                    ),
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "hourly_itinerary",
                    "strict": True,
                    "schema": ITINERARY_JSON_SCHEMA,
                }
            },
            "max_output_tokens": 4_000,
        }
        request = Request(
            self.endpoint,
            data=json.dumps(request_payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "CentralBlue-RouteController/0.1",
            },
        )
        try:
            with self._transport(request, timeout=self.timeout_seconds) as response:
                raw = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
        except HTTPError as error:
            detail = error.read(2_000).decode("utf-8", errors="replace")
            raise ItineraryProviderError(
                f"OpenAI returned HTTP {error.code}: {detail[:500]}"
            ) from error
        except (URLError, TimeoutError) as error:
            raise ItineraryProviderError(f"OpenAI could not be reached: {error}") from error
        if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
            raise ItineraryProviderError("OpenAI returned an unexpectedly large response")
        try:
            response_payload = json.loads(raw.decode("utf-8"))
            output_text = _response_output_text(response_payload)
            structured = json.loads(output_text)
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise ItineraryProviderError("OpenAI returned an invalid structured response") from error
        try:
            itinerary = parse_itinerary(structured)
        except ItineraryValidationError as error:
            raise ItineraryProviderError(
                f"OpenAI returned an unsafe itinerary: {error}"
            ) from error
        if itinerary.day.isoformat() != day or itinerary.timezone_name != timezone_name:
            raise ItineraryProviderError("OpenAI changed the requested date or timezone")
        return itinerary


class NominatimGeocoder:
    name = "OpenStreetMap Nominatim"

    def __init__(
        self,
        cache_path: Path,
        *,
        base_url: str = "https://nominatim.openstreetmap.org",
        timeout_seconds: float = 15,
        min_interval_seconds: float = 1.0,
        transport: Any = urlopen,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cache_path = cache_path
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.min_interval_seconds = min_interval_seconds
        self._transport = transport
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._last_request_at: float | None = None

    def geocode(self, place: ItineraryPlace) -> ResolvedPlace:
        cache_key = " ".join(place.query.casefold().split())
        with self._lock:
            cache = self._load_cache()
            cached = cache.get(cache_key)
            if isinstance(cached, dict):
                return self._resolved(place, cached)
            if self._last_request_at is not None:
                remaining = self.min_interval_seconds - (
                    self._clock() - self._last_request_at
                )
                if remaining > 0:
                    self._sleeper(remaining)
            query = urlencode({"q": place.query, "format": "jsonv2", "limit": 1})
            request = Request(
                f"{self.base_url}/search?{query}",
                headers={
                    "Accept": "application/json",
                    "User-Agent": "CentralBlue-RouteController/0.1 (local itinerary planner)",
                },
            )
            try:
                with self._transport(request, timeout=self.timeout_seconds) as response:
                    raw = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
            except HTTPError as error:
                raise GeocodingError(f"Nominatim returned HTTP {error.code}") from error
            except (URLError, TimeoutError) as error:
                raise GeocodingError(f"Nominatim could not be reached: {error}") from error
            finally:
                self._last_request_at = self._clock()
            if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
                raise GeocodingError("Nominatim returned an unexpectedly large response")
            try:
                results = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise GeocodingError("Nominatim returned invalid JSON") from error
            if not isinstance(results, list) or not results or not isinstance(results[0], dict):
                raise GeocodingError(f"No location matched {place.query!r}")
            entry = {
                "latitude": results[0].get("lat"),
                "longitude": results[0].get("lon"),
                "displayName": results[0].get("display_name"),
            }
            resolved = self._resolved(place, entry)
            cache[cache_key] = entry
            self._save_cache(cache)
            return resolved

    def _resolved(self, place: ItineraryPlace, entry: dict[str, Any]) -> ResolvedPlace:
        try:
            latitude = float(entry["latitude"])
            longitude = float(entry["longitude"])
        except (KeyError, TypeError, ValueError) as error:
            raise GeocodingError(f"Invalid coordinate returned for {place.query!r}") from error
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise GeocodingError(f"Invalid coordinate returned for {place.query!r}")
        display_name = entry.get("displayName")
        if not isinstance(display_name, str) or not display_name.strip():
            raise GeocodingError(f"Invalid place name returned for {place.query!r}")
        return ResolvedPlace(
            id=place.id,
            label=place.label,
            query=place.query,
            latitude=latitude,
            longitude=longitude,
            display_name=display_name.strip()[:500],
        )

    def _load_cache(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save_cache(self, cache: dict[str, Any]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_name(
            f".{self.cache_path.name}.{time.time_ns()}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            os.replace(temporary, self.cache_path)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise GeocodingError("The geocoding cache could not be saved") from error


def _parse_day(value: Any) -> date:
    if not isinstance(value, str):
        raise ItineraryValidationError("The itinerary date must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ItineraryValidationError("The itinerary date must use YYYY-MM-DD") from error


def _parse_timezone(value: Any) -> ZoneInfo:
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        raise ItineraryValidationError("Choose a valid IANA timezone")
    try:
        return ZoneInfo(value.strip())
    except ZoneInfoNotFoundError as error:
        raise ItineraryValidationError(f"Unknown timezone: {value}") from error


def _parse_local_time(value: Any, zone: ZoneInfo, label: str) -> datetime:
    if not isinstance(value, str) or not LOCAL_TIME_PATTERN.fullmatch(value):
        raise ItineraryValidationError(f"{label} must be a local ISO timestamp")
    try:
        return datetime.fromisoformat(value).replace(tzinfo=zone)
    except ValueError as error:
        raise ItineraryValidationError(f"{label} is invalid") from error


def _string(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ItineraryValidationError(f"{label} must be a non-empty string")
    value = value.strip()
    if len(value) > maximum:
        raise ItineraryValidationError(f"{label} must be {maximum} characters or fewer")
    return value


def _nullable_id(value: Any, ids: set[str], label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in ids:
        raise ItineraryValidationError(f"{label} references an unknown place")
    return value


def _coordinate(value: Any, minimum: float, maximum: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ItineraryValidationError(f"Resolved {label} must be a number")
    result = float(value)
    if not minimum <= result <= maximum:
        raise ItineraryValidationError(f"Resolved {label} is out of range")
    return result


def _segment_origin(segment: ItinerarySegment) -> str | None:
    return segment.place_id if segment.kind == "stay" else segment.origin_id


def _segment_destination(segment: ItinerarySegment) -> str | None:
    return segment.place_id if segment.kind == "stay" else segment.destination_id


def _route_point(place: ResolvedPlace, at: datetime, name: str) -> RoutePoint:
    return RoutePoint(
        latitude=place.latitude,
        longitude=place.longitude,
        time=at.astimezone(timezone.utc),
        name=name,
    )


def _response_output_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise TypeError("response is not an object")
    output = payload.get("output")
    if not isinstance(output, list):
        raise KeyError("output")
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "output_text":
                text = part.get("text")
                if isinstance(text, str):
                    return text
            if isinstance(part, dict) and part.get("type") == "refusal":
                raise ItineraryProviderError(str(part.get("refusal") or "Request refused"))
    raise KeyError("output_text")
