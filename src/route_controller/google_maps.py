from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .directions import DirectionsCoordinate


MAX_GOOGLE_MAPS_URL_LENGTH = 2_048
MAX_GOOGLE_MAPS_REDIRECTS = 5
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_COORDINATE_PAIR = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*,\s*"
    r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*$"
)
_DATA_COORDINATE_PAIR = re.compile(
    r"!3d([+-]?(?:\d+(?:\.\d*)?|\.\d+))"
    r"!4d([+-]?(?:\d+(?:\.\d*)?|\.\d+))"
)


class GoogleMapsLinkError(ValueError):
    """Base error for Google Maps directions-link intake."""


class GoogleMapsLinkValidationError(GoogleMapsLinkError):
    """Raised when the submitted URL is not a supported Maps directions URL."""


class GoogleMapsGeocodingRequiredError(GoogleMapsLinkError):
    """Raised when a directions link contains names rather than coordinates."""


class GoogleMapsLinkResolutionError(GoogleMapsLinkError):
    """Raised when a Google-owned short link cannot be expanded safely."""


@dataclass(frozen=True)
class GoogleMapsDirectionsLink:
    origin: DirectionsCoordinate
    destination: DirectionsCoordinate
    origin_label: str
    destination_label: str
    resolved_host: str
    was_shortened: bool


GoogleMapsLinkExpander = Callable[[str], str]


def resolve_google_maps_directions_link(
    value: str,
    *,
    expander: GoogleMapsLinkExpander | None = None,
) -> GoogleMapsDirectionsLink:
    """Resolve one coordinate-based Google Maps directions link."""
    submitted = _validated_url(value)
    was_shortened = _is_short_host(submitted.hostname or "", submitted.path)
    resolved_value = value.strip()
    if was_shortened:
        resolved_value = (expander or expand_google_maps_short_url)(resolved_value)

    parsed = _validated_url(resolved_value)
    if not _is_google_maps_host(parsed.hostname or ""):
        raise GoogleMapsLinkValidationError(
            "The resolved link is not hosted by Google Maps"
        )
    origin_value, destination_value = _directions_values(parsed)
    origin = _coordinate(origin_value)
    destination = _coordinate(destination_value)
    if origin is None or destination is None:
        raise GoogleMapsGeocodingRequiredError(
            "This Google Maps link uses place names or addresses. Phase 3 "
            "supports directions links whose origin and destination are "
            "latitude/longitude coordinates."
        )
    return GoogleMapsDirectionsLink(
        origin=origin,
        destination=destination,
        origin_label=_coordinate_label(origin),
        destination_label=_coordinate_label(destination),
        resolved_host=(parsed.hostname or "").lower(),
        was_shortened=was_shortened,
    )


def expand_google_maps_short_url(value: str) -> str:
    """Follow only Google-owned redirects and return the final Maps URL."""
    current = value.strip()
    opener = build_opener(_NoRedirectHandler())
    for _ in range(MAX_GOOGLE_MAPS_REDIRECTS + 1):
        parsed = _validated_url(current)
        if not _is_google_redirect_host(parsed.hostname or ""):
            raise GoogleMapsLinkResolutionError(
                "The Google Maps short link redirected outside Google"
            )
        request = Request(
            current,
            headers={
                "Accept": "text/html",
                "Range": "bytes=0-0",
                "User-Agent": "CentralBlue-RouteController/0.1",
            },
            method="GET",
        )
        try:
            response = opener.open(request, timeout=10)
        except HTTPError as error:
            if error.code not in _REDIRECT_STATUSES:
                raise GoogleMapsLinkResolutionError(
                    f"Google Maps returned HTTP {error.code} while expanding the link"
                ) from error
            location = error.headers.get("Location")
            if not location:
                raise GoogleMapsLinkResolutionError(
                    "Google Maps returned a redirect without a destination"
                ) from error
            next_url = urljoin(current, location)
            try:
                next_parsed = _validated_url(next_url)
            except GoogleMapsLinkValidationError as validation_error:
                raise GoogleMapsLinkResolutionError(
                    "The Google Maps short link redirected outside Google"
                ) from validation_error
            if not _is_google_redirect_host(next_parsed.hostname or ""):
                raise GoogleMapsLinkResolutionError(
                    "The Google Maps short link redirected outside Google"
                )
            current = next_url
            continue
        except (URLError, TimeoutError) as error:
            reason = getattr(error, "reason", error)
            raise GoogleMapsLinkResolutionError(
                f"The Google Maps short link could not be reached: {reason}"
            ) from error
        else:
            final_url = response.geturl()
            response.close()
            final = _validated_url(final_url)
            if _is_short_host(final.hostname or "", final.path):
                raise GoogleMapsLinkResolutionError(
                    "The Google Maps short link did not resolve to directions"
                )
            return final_url
    raise GoogleMapsLinkResolutionError(
        "The Google Maps short link used too many redirects"
    )


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _validated_url(value: str):
    if not isinstance(value, str) or not value.strip():
        raise GoogleMapsLinkValidationError(
            "'url' must be a non-empty Google Maps directions URL"
        )
    cleaned = value.strip()
    if len(cleaned) > MAX_GOOGLE_MAPS_URL_LENGTH:
        raise GoogleMapsLinkValidationError(
            "Google Maps URLs must be 2048 characters or fewer"
        )
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() != "https":
        raise GoogleMapsLinkValidationError(
            "Google Maps links must use HTTPS"
        )
    try:
        port = parsed.port
    except ValueError as error:
        raise GoogleMapsLinkValidationError(
            "The Google Maps link has an invalid port"
        ) from error
    if parsed.username or parsed.password or port not in (None, 443):
        raise GoogleMapsLinkValidationError(
            "Google Maps links cannot contain credentials or a custom port"
        )
    if not _is_google_redirect_host(parsed.hostname or ""):
        raise GoogleMapsLinkValidationError(
            "Only Google Maps directions links are supported"
        )
    return parsed


def _directions_values(parsed) -> tuple[str, str]:
    query = parse_qs(parsed.query, keep_blank_values=True)
    if query.get("origin") and query.get("destination"):
        if query.get("api", [""])[0] not in ("", "1"):
            raise GoogleMapsLinkValidationError(
                "The Google Maps URL has an unsupported API version"
            )
        return query["origin"][0], query["destination"][0]

    path = unquote(parsed.path)
    marker = "/maps/dir/"
    if marker not in path:
        raise GoogleMapsLinkValidationError(
            "The link must describe Google Maps directions"
        )
    route_path = path.split(marker, 1)[1]
    segments = [
        segment
        for segment in route_path.split("/")
        if segment and not segment.startswith("@") and segment != "data="
    ]
    if (
        len(segments) >= 2
        and _coordinate(segments[0].replace("+", " ")) is not None
        and _coordinate(segments[1].replace("+", " ")) is not None
    ):
        return segments[0].replace("+", " "), segments[1].replace("+", " ")

    data_pairs = _DATA_COORDINATE_PAIR.findall(unquote(parsed.geturl()))
    if len(data_pairs) >= 2:
        return ",".join(data_pairs[0]), ",".join(data_pairs[-1])
    if len(segments) >= 2:
        return segments[0].replace("+", " "), segments[1].replace("+", " ")
    raise GoogleMapsLinkValidationError(
        "The Google Maps directions link does not include both endpoints"
    )


def _coordinate(value: str) -> DirectionsCoordinate | None:
    match = _COORDINATE_PAIR.fullmatch(value)
    if match is None:
        return None
    latitude = float(match.group(1))
    longitude = float(match.group(2))
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise GoogleMapsLinkValidationError(
            "A Google Maps endpoint coordinate is outside the valid range"
        )
    return DirectionsCoordinate(latitude=latitude, longitude=longitude)


def _coordinate_label(coordinate: DirectionsCoordinate) -> str:
    return f"{coordinate.latitude:.7f}, {coordinate.longitude:.7f}"


def _is_google_maps_host(hostname: str) -> bool:
    host = hostname.lower().rstrip(".")
    return host == "google.com" or host.endswith(".google.com")


def _is_google_redirect_host(hostname: str) -> bool:
    host = hostname.lower().rstrip(".")
    return (
        _is_google_maps_host(host)
        or host == "goo.gl"
        or host.endswith(".goo.gl")
    )


def _is_short_host(hostname: str, path: str) -> bool:
    host = hostname.lower().rstrip(".")
    return host == "maps.app.goo.gl" or (
        host == "goo.gl" and path.startswith("/maps")
    )
