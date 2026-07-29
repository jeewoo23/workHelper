from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


class RouteRegistryError(ValueError):
    """Raised when the local route registry is invalid."""


@dataclass(frozen=True)
class RouteRecord:
    id: str
    label: str
    direction: str
    track_path: Path
    origin_label: str = ""
    destination_label: str = ""
    source_path: Optional[Path] = None
    created_at: str = ""
    bundled: bool = False


class RouteRegistry:
    def __init__(self, root: Path, registry_path: Optional[Path] = None) -> None:
        self.root = root.resolve()
        self.registry_path = registry_path or self.root / "routes" / "routes.json"

    def all(self) -> list[RouteRecord]:
        if self.registry_path.exists():
            return list(self._load_registry())
        return self._bundled_routes()

    def get(self, route_id: str) -> RouteRecord:
        for route in self.all():
            if route.id == route_id:
                return route
        raise RouteRegistryError(f"Unknown route: {route_id}")

    def _load_registry(self) -> Iterable[RouteRecord]:
        try:
            payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise RouteRegistryError(
                f"Route registry is invalid JSON: {self.registry_path}"
            ) from error

        routes = payload.get("routes")
        if not isinstance(routes, list):
            raise RouteRegistryError("Route registry must contain a 'routes' list")

        seen: set[str] = set()
        for index, item in enumerate(routes):
            if not isinstance(item, dict):
                raise RouteRegistryError(f"Route registry item {index} is not an object")
            route = self._record_from_payload(item, index=index)
            if route.id in seen:
                raise RouteRegistryError(f"Duplicate route id in registry: {route.id}")
            seen.add(route.id)
            yield route

    def _record_from_payload(self, item: dict[str, Any], *, index: int) -> RouteRecord:
        route_id = _required_string(item, "id", index)
        label = _required_string(item, "label", index)
        direction = _required_string(item, "direction", index)
        track_path = self._resolve_project_path(_required_string(item, "trackPath", index))
        if not track_path.is_file():
            raise RouteRegistryError(f"Route {route_id!r} track does not exist: {track_path}")

        source_path = None
        source_text = item.get("sourcePath")
        if isinstance(source_text, str) and source_text:
            source_path = self._resolve_project_path(source_text)

        return RouteRecord(
            id=route_id,
            label=label,
            direction=direction,
            track_path=track_path,
            origin_label=str(item.get("originLabel") or ""),
            destination_label=str(item.get("destinationLabel") or ""),
            source_path=source_path,
            created_at=str(item.get("createdAt") or ""),
            bundled=bool(item.get("bundled", False)),
        )

    def _resolve_project_path(self, value: str) -> Path:
        candidate = (self.root / value).resolve()
        if not candidate.is_relative_to(self.root):
            raise RouteRegistryError(f"Route path escapes project root: {value}")
        return candidate

    def _bundled_routes(self) -> list[RouteRecord]:
        return [
            RouteRecord(
                id="l1-to-l2",
                label="L1 to L2",
                direction="outbound",
                origin_label="L1",
                destination_label="L2",
                track_path=self.root / "routes" / "tracks" / "route_L1_to_L2.track.gpx",
                source_path=self.root / "routes" / "source" / "route_final.gpx",
                bundled=True,
            ),
            RouteRecord(
                id="l2-to-l1",
                label="L2 to L1",
                direction="inbound",
                origin_label="L2",
                destination_label="L1",
                track_path=self.root / "routes" / "tracks" / "route_L2_to_L1.track.gpx",
                source_path=self.root / "routes" / "source" / "route_final.gpx",
                bundled=True,
            ),
        ]


def _required_string(item: dict[str, Any], key: str, index: int) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RouteRegistryError(f"Route registry item {index} is missing {key!r}")
    return value.strip()
