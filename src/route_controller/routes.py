from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
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
    timing_mode: str = ""
    timing_provider: str = ""
    estimated_duration_seconds: Optional[float] = None
    timing_warning: str = ""


class RouteRegistry:
    def __init__(self, root: Path, registry_path: Optional[Path] = None) -> None:
        self.root = root.resolve()
        self.registry_path = registry_path or self.root / "routes" / "routes.json"
        self._write_lock = Lock()

    def all(self) -> list[RouteRecord]:
        if self.registry_path.exists():
            return list(self._load_registry())
        return self._bundled_routes()

    def get(self, route_id: str) -> RouteRecord:
        for route in self.all():
            if route.id == route_id:
                return route
        raise RouteRegistryError(f"Unknown route: {route_id}")

    def upsert(self, route: RouteRecord) -> RouteRecord:
        if not route.id.strip() or not route.label.strip() or not route.direction.strip():
            raise RouteRegistryError("Generated route id, label, and direction are required")
        track_path = route.track_path.resolve()
        if not track_path.is_relative_to(self.root):
            raise RouteRegistryError("Generated track path escapes project root")
        if not track_path.is_file():
            raise RouteRegistryError(
                f"Generated route track does not exist: {track_path}"
            )
        if route.source_path is not None:
            source_path = route.source_path.resolve()
            if not source_path.is_relative_to(self.root):
                raise RouteRegistryError("Generated source path escapes project root")

        with self._write_lock:
            payload = self._registry_payload_for_write()
            routes = payload["routes"]
            record_payload = self._payload_from_record(route)
            for index, existing in enumerate(routes):
                if isinstance(existing, dict) and existing.get("id") == route.id:
                    routes[index] = record_payload
                    break
            else:
                routes.append(record_payload)

            self.registry_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = self.registry_path.with_suffix(
                f"{self.registry_path.suffix}.tmp"
            )
            temporary_path.write_text(
                json.dumps(payload, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary_path.replace(self.registry_path)
        return self.get(route.id)

    def remove(self, route_id: str) -> Optional[RouteRecord]:
        with self._write_lock:
            payload = self._registry_payload_for_write()
            routes = payload["routes"]
            removed_payload = next(
                (
                    item
                    for item in routes
                    if isinstance(item, dict) and item.get("id") == route_id
                ),
                None,
            )
            if removed_payload is None:
                return None
            removed = self._record_from_payload(removed_payload, index=0)
            payload["routes"] = [
                item
                for item in routes
                if not (
                    isinstance(item, dict)
                    and item.get("id") == route_id
                )
            ]
            temporary_path = self.registry_path.with_suffix(
                f"{self.registry_path.suffix}.tmp"
            )
            temporary_path.write_text(
                json.dumps(payload, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary_path.replace(self.registry_path)
        return removed

    def _registry_payload_for_write(self) -> dict[str, Any]:
        if not self.registry_path.exists():
            return {"version": 1, "routes": []}
        try:
            payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise RouteRegistryError(
                f"Route registry is invalid JSON: {self.registry_path}"
            ) from error
        if not isinstance(payload, dict) or not isinstance(payload.get("routes"), list):
            raise RouteRegistryError("Route registry must contain a 'routes' list")
        return payload

    def _payload_from_record(self, route: RouteRecord) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": route.id,
            "label": route.label,
            "direction": route.direction,
            "originLabel": route.origin_label,
            "destinationLabel": route.destination_label,
            "trackPath": str(route.track_path.resolve().relative_to(self.root)),
            "createdAt": route.created_at,
            "bundled": route.bundled,
        }
        if route.source_path is not None:
            payload["sourcePath"] = str(
                route.source_path.resolve().relative_to(self.root)
            )
        if route.timing_mode:
            payload["timingMode"] = route.timing_mode
        if route.timing_provider:
            payload["timingProvider"] = route.timing_provider
        if route.estimated_duration_seconds is not None:
            payload["estimatedDurationSeconds"] = route.estimated_duration_seconds
        if route.timing_warning:
            payload["timingWarning"] = route.timing_warning
        return payload

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

        bundled = bool(item.get("bundled", False))
        estimated_duration = item.get("estimatedDurationSeconds")
        if (
            isinstance(estimated_duration, bool)
            or not isinstance(estimated_duration, (int, float))
        ):
            estimated_duration = None

        return RouteRecord(
            id=route_id,
            label=label,
            direction=direction,
            track_path=track_path,
            origin_label=str(item.get("originLabel") or ""),
            destination_label=str(item.get("destinationLabel") or ""),
            source_path=source_path,
            created_at=str(item.get("createdAt") or ""),
            bundled=bundled,
            timing_mode=str(
                item.get("timingMode") or ("source" if bundled else "")
            ),
            timing_provider=str(item.get("timingProvider") or ""),
            estimated_duration_seconds=(
                float(estimated_duration)
                if estimated_duration is not None
                else None
            ),
            timing_warning=str(item.get("timingWarning") or ""),
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
                timing_mode="source",
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
                timing_mode="source",
            ),
        ]


def _required_string(item: dict[str, Any], key: str, index: int) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RouteRegistryError(f"Route registry item {index} is missing {key!r}")
    return value.strip()
