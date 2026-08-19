from __future__ import annotations

import json
import mimetypes
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Callable, Optional
from urllib.parse import unquote, urlparse

from .directions import (
    DirectionsCoordinate,
    DirectionsProvider,
    DirectionsProviderError,
    DirectionsValidationError,
    OsrmDirectionsProvider,
    generate_route,
)
from .environment import DeviceTarget, EnvironmentReport, inspect_environment
from .gpx import (
    GpxValidationError,
    RoutePoint,
    inspect_gpx_content,
    parse_track,
    prepare_gpx_playback_result,
    summarize,
    track_xml,
    write_track,
)
from .google_maps import (
    GoogleMapsGeocodingRequiredError,
    GoogleMapsLinkExpander,
    GoogleMapsLinkResolutionError,
    GoogleMapsLinkValidationError,
    resolve_google_maps_directions_link,
)
from .playback import (
    clear_arguments,
    play_arguments,
    resolve_executable,
    set_arguments,
)
from .routes import RouteRecord, RouteRegistry, RouteRegistryError
from .schedule import (
    LocationScheduleController,
    ScheduleStorageError,
    ScheduleValidationError,
)
from .timing import (
    DEFAULT_OSRM_URL,
    OsrmRouteTimingProvider,
    RoadTimingProvider,
)


ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = ROOT / "frontend"
IMPORTS_DIR = ROOT / "routes" / "imports"
GENERATED_DIR = ROOT / "routes" / "generated"
SCHEDULE_PATH = ROOT / "routes" / "schedules" / "location-schedule.json"
MAX_GPX_CONTENT_BYTES = 5 * 1024 * 1024
MAX_JSON_REQUEST_BYTES = MAX_GPX_CONTENT_BYTES + (1024 * 1024)
MAX_ROUTE_PREVIEW_POINTS = 8000
DEFAULT_REGISTRY = RouteRegistry(ROOT)
DEFAULT_TIMING_PROVIDER = OsrmRouteTimingProvider(
    os.environ.get("ROUTE_CONTROLLER_OSRM_URL") or DEFAULT_OSRM_URL
)
DEFAULT_DIRECTIONS_PROVIDER = OsrmDirectionsProvider(
    os.environ.get("ROUTE_CONTROLLER_OSRM_URL") or DEFAULT_OSRM_URL
)
BACKEND_CAPABILITIES = {
    "deleteImports": True,
    "deviceSelection": True,
    "directionsGeneration": True,
    "googleMapsLinks": True,
    "locationScheduling": True,
    "multipleLocationSchedules": True,
    "nullableDuration": True,
    "routeAwareTiming": True,
    "staticLocation": True,
    "staticLocationHeartbeat": True,
}


@dataclass
class ActivePlayback:
    route_id: str
    label: str
    started_at: float
    duration_seconds: float
    process: subprocess.Popen[str]
    device_id: str
    device_class: str
    power_process: Optional[subprocess.Popen[str]] = None
    power_warning: str = ""
    paused_at: Optional[float] = None
    total_paused_seconds: float = 0.0


def friendly_device_error(
    message: str, *, device_class: str = "device", os_name: str = "OS"
) -> str:
    lower = message.lower()
    noun = device_class if device_class in ("iPhone", "iPad") else "device"
    if "unable to connect to tunneld" in lower:
        return (
            f"The {noun} developer tunnel is not running. Start it with "
            "`sudo python3 -m pymobiledevice3 remote tunneld`, or use the userspace "
            "tunnel if your command supports it."
        )
    if "no usb-connected" in lower or "no device" in lower:
        return f"No USB {noun} was detected. Connect and unlock it, then tap Trust if {os_name} asks."
    if "developer mode" in lower:
        return f"Developer Mode is not available on the {noun}. Enable it in Settings and reconnect the device."
    if "developerdiskimage" in lower or "mount" in lower:
        return f"The {noun} developer image is not mounted. Try `uv run pymobiledevice3 mounter auto-mount`, then retry."
    if "pass --userspace" in lower or "no-root tunnel" in lower:
        return f"This {os_name} version needs the userspace developer tunnel. Reconnect the {noun} and retry."
    if "lockdown" in lower or "invalid hostid" in lower or "pair" in lower:
        return f"The {noun} pairing is not ready. Unlock it, confirm Trust This Computer, then retry."
    if "timed out" in lower or "timeout" in lower:
        return f"The device command timed out. Keep the {noun} unlocked and connected, then retry."
    return message.strip() or "The device command failed."


class PlaybackManager:
    def __init__(
        self,
        *,
        userspace: bool = True,
        registry: Optional[RouteRegistry] = None,
        device_provider: Optional[Callable[[], EnvironmentReport]] = None,
        static_heartbeat_interval_seconds: float = 300,
        static_watchdog_poll_seconds: float = 5,
        process_startup_grace_seconds: float = 0.25,
    ) -> None:
        self._lock = Lock()
        self._active: Optional[ActivePlayback] = None
        self._static_process: Optional[subprocess.Popen[str]] = None
        self._static_power_process: Optional[subprocess.Popen[str]] = None
        self._static_power_warning = ""
        self._static_stop_event: Optional[Event] = None
        self._static_generation = 0
        self._static_last_reasserted_at: Optional[str] = None
        self._static_last_reasserted_monotonic: Optional[float] = None
        self._static_reassertion_count = 0
        self._static_heartbeat_interval_seconds = max(
            0.01, float(static_heartbeat_interval_seconds)
        )
        self._static_watchdog_poll_seconds = max(
            0.01, float(static_watchdog_poll_seconds)
        )
        self._process_startup_grace_seconds = max(
            0.0, float(process_startup_grace_seconds)
        )
        self._userspace = userspace
        self._last_error: Optional[dict[str, str]] = None
        self._static_location: Optional[dict[str, float]] = None
        self._static_device: Optional[DeviceTarget] = None
        self._registry = registry or DEFAULT_REGISTRY
        self._device_provider = device_provider or (
            lambda: inspect_environment(probe_device=True)
        )
        self._selected_device_id: Optional[str] = None

    def device_status(self) -> dict[str, Any]:
        report = self._refresh_devices()
        with self._lock:
            compatible = [device for device in report.devices if device.compatible]
            selected = next(
                (
                    device
                    for device in compatible
                    if device.identifier == self._selected_device_id
                ),
                None,
            )
            payload = report.as_dict()
            payload.update(
                {
                    "selectedDeviceId": self._selected_device_id,
                    "selectedDevice": selected.as_dict() if selected else None,
                    "selectionRequired": selected is None and len(compatible) > 0,
                }
            )
            return payload

    def select_device(self, device_id: str) -> dict[str, Any]:
        if not isinstance(device_id, str) or not device_id.strip():
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "A device identifier is required",
                code="invalid_device_selection",
            )
        report = self._refresh_devices()
        with self._lock:
            self._reap_locked()
            self._reap_static_locked()
            if self._active is not None or self._static_location is not None:
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    "Stop playback and clear the static location before changing devices",
                    code="device_busy",
                )
            selected = next(
                (
                    device
                    for device in report.devices
                    if device.identifier == device_id and device.compatible
                ),
                None,
            )
            if selected is None:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "That compatible USB device is no longer available",
                    code="device_unavailable",
                )
            self._selected_device_id = selected.identifier
        return self.device_status()

    def _refresh_devices(self) -> EnvironmentReport:
        report = self._device_provider()
        with self._lock:
            compatible = [device for device in report.devices if device.compatible]
            if self._selected_device_id is None and len(compatible) == 1:
                self._selected_device_id = compatible[0].identifier
        return report

    def _require_target(self) -> DeviceTarget:
        report = self._refresh_devices()
        with self._lock:
            compatible = [device for device in report.devices if device.compatible]
            selected = next(
                (
                    device
                    for device in compatible
                    if device.identifier == self._selected_device_id
                ),
                None,
            )
            if selected is not None:
                return selected
            if compatible:
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    "The previously selected device is unavailable. Choose which USB iPhone or iPad to control",
                    code="device_selection_required",
                )
            message = report.device_probe_output or (
                "No compatible USB iPhone or iPad is connected"
            )
            raise ApiError(
                HTTPStatus.CONFLICT,
                message,
                code="device_unavailable",
            )

    def _assert_target_selected_locked(self, target: DeviceTarget) -> None:
        if self._selected_device_id != target.identifier:
            raise ApiError(
                HTTPStatus.CONFLICT,
                "The selected device changed; retry the operation",
                code="device_selection_changed",
            )

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._reap_locked()
            self._reap_static_locked()
            return self._status_locked()

    def start(self, route_id: str) -> dict[str, Any]:
        try:
            route = self._registry.get(route_id)
        except RouteRegistryError:
            raise ApiError(HTTPStatus.NOT_FOUND, f"Unknown route: {route_id}")

        target = self._require_target()
        with self._lock:
            self._assert_target_selected_locked(target)
            self._reap_locked()
            self._reap_static_locked()
            if self._active is not None:
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    f"{self._active.label} is already playing",
                )
            self._stop_static_locked()

            command_executable = resolve_executable(None)
            arguments = play_arguments(
                command_executable,
                route.track_path,
                userspace=self._userspace,
                udid=target.identifier,
            )
            name, points = parse_track(route.track_path)
            summary = summarize(name, points)
            try:
                process = subprocess.Popen(
                    arguments,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except OSError as error:
                raise ApiError(
                    HTTPStatus.BAD_GATEWAY,
                    friendly_device_error(
                        str(error),
                        device_class=target.device_class,
                        os_name=target.os_name,
                    ),
                    detail=str(error),
                    code="device_command_failed",
                ) from error
            self._active = ActivePlayback(
                route_id=route_id,
                label=route.label,
                started_at=time.monotonic(),
                duration_seconds=summary.duration_seconds,
                process=process,
                device_id=target.identifier,
                device_class=target.device_class,
            )
            try:
                self._active.power_process = subprocess.Popen(
                    ["caffeinate", "-i", "-w", str(process.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
            except OSError as error:
                self._active.power_process = None
                self._active.power_warning = (
                    f"Mac idle-sleep prevention could not start: {error}"
                )
            self._static_location = None
            return self._status_locked()

    def pause(self) -> dict[str, Any]:
        with self._lock:
            self._reap_locked()
            if self._active is None:
                raise ApiError(HTTPStatus.CONFLICT, "No route is playing")
            if self._active.paused_at is None:
                self._active.process.send_signal(signal.SIGSTOP)
                self._active.paused_at = time.monotonic()
            return self._status_locked()

    def resume(self) -> dict[str, Any]:
        with self._lock:
            self._reap_locked()
            if self._active is None:
                raise ApiError(HTTPStatus.CONFLICT, "No route is playing")
            if self._active.paused_at is not None:
                paused_for = time.monotonic() - self._active.paused_at
                self._active.total_paused_seconds += paused_for
                self._active.paused_at = None
                self._active.process.send_signal(signal.SIGCONT)
            return self._status_locked()

    def stop(self, *, clear_location: bool) -> dict[str, Any]:
        stopped_active_process = False
        with self._lock:
            self._reap_locked()
            self._reap_static_locked()
            static_process_active = self._static_location is not None
            active = self._active
            if active is not None:
                active.process.terminate()
                try:
                    active.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    active.process.kill()
                    active.process.wait(timeout=5)
                self._stop_power_assertion(active)
                self._active = None
                stopped_active_process = True

        if clear_location and (stopped_active_process or static_process_active):
            self.clear_location()
        return self.status()

    def clear_location(self) -> dict[str, Any]:
        try:
            target = self._require_target()
        except ApiError:
            with self._lock:
                self._reap_static_locked()
                self._stop_static_locked()
            raise
        with self._lock:
            self._assert_target_selected_locked(target)
            self._reap_static_locked()
            self._stop_static_locked()
        command_executable = resolve_executable(None)
        result = subprocess.run(
            clear_arguments(
                command_executable,
                userspace=self._userspace,
                udid=target.identifier,
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "clear failed"
            raise ApiError(
                HTTPStatus.BAD_GATEWAY,
                friendly_device_error(
                    message,
                    device_class=target.device_class,
                    os_name=target.os_name,
                ),
                detail=message,
                code="device_clear_failed",
            )
        self._last_error = None
        with self._lock:
            self._static_location = None
        return {"state": "cleared"}

    def set_location(
        self,
        latitude: float,
        longitude: float,
    ) -> dict[str, Any]:
        target = self._require_target()
        with self._lock:
            self._assert_target_selected_locked(target)
            self._reap_locked()
            self._reap_static_locked()
            if self._active is not None:
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    "Stop route playback before activating a static position",
                    code="playback_active",
                )
            self._stop_static_locked()
            try:
                process = self._launch_static_process_locked(
                    target,
                    latitude,
                    longitude,
                )
            except ApiError:
                self._stop_static_locked()
                raise
            self._static_process = process
            self._static_device = target
            self._static_location = {
                "latitude": latitude,
                "longitude": longitude,
            }
            self._record_static_reassertion_locked()
            self._ensure_static_power_assertion_locked()
            self._static_generation += 1
            generation = self._static_generation
            stop_event = Event()
            self._static_stop_event = stop_event
            Thread(
                target=self._static_watchdog,
                args=(generation, stop_event),
                daemon=True,
                name="static-location-watchdog",
            ).start()
            self._last_error = None
            return self._simulated_location_locked()

    def simulated_location(self) -> Optional[dict[str, Any]]:
        with self._lock:
            self._reap_static_locked()
            return self._simulated_location_locked()

    def _launch_static_process_locked(
        self,
        target: DeviceTarget,
        latitude: float,
        longitude: float,
    ) -> subprocess.Popen[str]:
        command_executable = resolve_executable(None)
        arguments = set_arguments(
            command_executable,
            latitude,
            longitude,
            userspace=self._userspace,
            udid=target.identifier,
        )
        try:
            process = subprocess.Popen(
                arguments,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as error:
            raise ApiError(
                HTTPStatus.BAD_GATEWAY,
                friendly_device_error(
                    str(error),
                    device_class=target.device_class,
                    os_name=target.os_name,
                ),
                detail=str(error),
                code="device_set_failed",
            ) from error
        if self._process_startup_grace_seconds:
            time.sleep(self._process_startup_grace_seconds)
        returncode = process.poll()
        if returncode is not None:
            raw_error = self._process_stderr(process)
            message = friendly_device_error(
                raw_error or f"Static location exited with code {returncode}",
                device_class=target.device_class,
                os_name=target.os_name,
            )
            raise ApiError(
                HTTPStatus.BAD_GATEWAY,
                message,
                detail=raw_error,
                code="device_set_failed",
            )
        return process

    def _static_watchdog(self, generation: int, stop_event: Event) -> None:
        while not stop_event.wait(self._static_watchdog_poll_seconds):
            with self._lock:
                if (
                    generation != self._static_generation
                    or self._static_stop_event is not stop_event
                    or self._static_location is None
                    or self._static_device is None
                ):
                    return
                self._reap_static_locked()
                self._ensure_static_power_assertion_locked()
                elapsed = (
                    time.monotonic() - self._static_last_reasserted_monotonic
                    if self._static_last_reasserted_monotonic is not None
                    else self._static_heartbeat_interval_seconds
                )
                if (
                    self._static_process is None
                    or elapsed >= self._static_heartbeat_interval_seconds
                ):
                    self._reassert_static_location_locked()

    def _reassert_static_location_locked(self) -> None:
        target = self._static_device
        location = self._static_location
        if target is None or location is None:
            return
        self._terminate_process(self._static_process)
        self._static_process = None
        try:
            self._static_process = self._launch_static_process_locked(
                target,
                location["latitude"],
                location["longitude"],
            )
        except ApiError as error:
            self._last_error = {
                "code": error.code,
                "message": error.message,
                "detail": error.detail,
            }
            return
        self._record_static_reassertion_locked()
        if self._last_error and self._last_error.get("code") == "device_set_failed":
            self._last_error = None

    def _record_static_reassertion_locked(self) -> None:
        self._static_last_reasserted_monotonic = time.monotonic()
        self._static_last_reasserted_at = (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        self._static_reassertion_count += 1

    def _ensure_static_power_assertion_locked(self) -> None:
        if (
            self._static_power_process is not None
            and self._static_power_process.poll() is None
        ):
            return
        self._static_power_process = None
        try:
            self._static_power_process = subprocess.Popen(
                ["caffeinate", "-i", "-w", str(os.getpid())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            self._static_power_warning = ""
        except OSError as error:
            self._static_power_warning = (
                f"Mac idle-sleep prevention could not start: {error}"
            )

    def _simulated_location_locked(self) -> Optional[dict[str, Any]]:
        if self._static_location is None:
            return None
        power_process = self._static_power_process
        preventing_idle_sleep = (
            power_process is not None and power_process.poll() is None
        )
        payload: dict[str, Any] = {
            **self._static_location,
            "state": "active" if self._static_process is not None else "recovering",
            "heartbeatIntervalSeconds": self._static_heartbeat_interval_seconds,
            "lastReassertedAt": self._static_last_reasserted_at,
            "reassertionCount": self._static_reassertion_count,
            "preventingIdleSleep": preventing_idle_sleep,
        }
        if self._static_power_warning:
            payload["powerWarning"] = self._static_power_warning
        return payload

    def _reap_static_locked(self) -> None:
        if self._static_process is None:
            return
        returncode = self._static_process.poll()
        if returncode is None:
            return
        if returncode != 0:
            raw_error = self._process_stderr(self._static_process)
            target = self._static_device
            self._last_error = {
                "code": "device_set_failed",
                "message": friendly_device_error(
                    raw_error or f"Static location exited with code {returncode}",
                    device_class=target.device_class if target else "device",
                    os_name=target.os_name if target else "OS",
                ),
                "detail": raw_error,
            }
        self._static_process = None

    def _stop_static_locked(self) -> None:
        self._static_generation += 1
        if self._static_stop_event is not None:
            self._static_stop_event.set()
        self._static_stop_event = None
        self._terminate_process(self._static_process)
        self._terminate_process(self._static_power_process, timeout=2)
        self._static_process = None
        self._static_power_process = None
        self._static_power_warning = ""
        self._static_device = None
        self._static_location = None
        self._static_last_reasserted_at = None
        self._static_last_reasserted_monotonic = None
        self._static_reassertion_count = 0

    @staticmethod
    def _terminate_process(
        process: Optional[subprocess.Popen[str]], *, timeout: float = 5
    ) -> None:
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=timeout)

    def _reap_locked(self) -> None:
        if self._active is None:
            return
        returncode = self._active.process.poll()
        if returncode is not None:
            if returncode != 0:
                raw_error = self._process_stderr(self._active.process)
                message = friendly_device_error(
                    raw_error or f"Route playback exited with code {returncode}",
                    device_class=self._active.device_class,
                    os_name="iPadOS" if self._active.device_class == "iPad" else "iOS",
                )
                self._last_error = {
                    "code": "playback_failed",
                    "message": message,
                    "detail": raw_error,
                }
            self._stop_power_assertion(self._active)
            self._active = None

    @staticmethod
    def _stop_power_assertion(active: ActivePlayback) -> None:
        process = active.power_process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    @staticmethod
    def _process_stderr(process: subprocess.Popen[str]) -> str:
        stderr = getattr(process, "stderr", None)
        if stderr is None:
            return ""
        try:
            return stderr.read().strip()
        except Exception:
            return ""

    def _status_locked(self) -> dict[str, Any]:
        if self._active is None:
            status: dict[str, Any] = {"state": "idle"}
            if self._last_error:
                status["error"] = self._last_error
            return status

        now = time.monotonic()
        paused_seconds = self._active.total_paused_seconds
        if self._active.paused_at is not None:
            paused_seconds += now - self._active.paused_at
        elapsed = max(0.0, now - self._active.started_at - paused_seconds)
        progress = min(1.0, elapsed / self._active.duration_seconds)
        power_process = self._active.power_process
        preventing_idle_sleep = (
            power_process is not None and power_process.poll() is None
        )
        power_warning = self._active.power_warning
        if power_process is not None and not preventing_idle_sleep and not power_warning:
            power_warning = "Mac idle-sleep prevention ended before playback"
        status = {
            "state": "paused" if self._active.paused_at is not None else "playing",
            "routeId": self._active.route_id,
            "label": self._active.label,
            "startedAt": self._active.started_at,
            "elapsedSeconds": elapsed,
            "durationSeconds": self._active.duration_seconds,
            "progress": progress,
            "pid": self._active.process.pid,
            "deviceId": self._active.device_id,
            "deviceClass": self._active.device_class,
            "preventingIdleSleep": preventing_idle_sleep,
        }
        if power_warning:
            status["powerWarning"] = power_warning
        return status


class ApiError(Exception):
    def __init__(
        self,
        status: HTTPStatus,
        message: str,
        *,
        detail: str = "",
        code: str = "request_failed",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail
        self.code = code


class RouteRequestHandler(BaseHTTPRequestHandler):
    manager: PlaybackManager
    registry = DEFAULT_REGISTRY
    imports_directory = IMPORTS_DIR
    generated_directory = GENERATED_DIR
    timing_provider: Optional[RoadTimingProvider] = DEFAULT_TIMING_PROVIDER
    directions_provider: Optional[DirectionsProvider] = DEFAULT_DIRECTIONS_PROVIDER
    schedule_controller: Optional[LocationScheduleController] = None
    google_maps_link_expander: Optional[GoogleMapsLinkExpander] = None

    def do_HEAD(self) -> None:
        try:
            parsed = urlparse(self.path)
            relative = "index.html" if parsed.path in ("", "/") else parsed.path.lstrip("/")
            target = (FRONTEND_DIR / relative).resolve()
            if not target.is_relative_to(FRONTEND_DIR.resolve()) or not target.is_file():
                raise ApiError(HTTPStatus.NOT_FOUND, "Not found")
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(target.stat().st_size))
            self.end_headers()
        except ApiError as error:
            self._send_error(error)

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/status":
                self._send_json(
                    {
                        "apiVersion": 6,
                        "capabilities": BACKEND_CAPABILITIES,
                        "device": self.manager.device_status(),
                        "playback": self.manager.status(),
                        "simulatedLocation": self.manager.simulated_location(),
                        "schedule": (
                            self.schedule_controller.status()
                            if self.schedule_controller is not None
                            else _disabled_schedule_status()
                        ),
                    }
                )
                return
            if parsed.path == "/api/routes":
                self._send_json({"routes": route_payloads(self.registry)})
                return
            if parsed.path == "/api/routes/imports":
                self._send_json(
                    {"imports": imported_gpx_payloads(self.imports_directory)}
                )
                return
            if parsed.path.startswith("/api/routes/imports/"):
                filename = unquote(parsed.path.removeprefix("/api/routes/imports/"))
                self._send_json(
                    imported_gpx_detail(
                        filename,
                        imports_directory=self.imports_directory,
                    )
                )
                return
            if (
                parsed.path.startswith("/api/routes/")
                and parsed.path.endswith("/preview")
            ):
                route_id = unquote(
                    parsed.path.removeprefix("/api/routes/").removesuffix(
                        "/preview"
                    )
                )
                self._send_json(route_preview_payload(route_id, self.registry))
                return
            if parsed.path.startswith("/api/routes/"):
                route_id = unquote(parsed.path.rsplit("/", 1)[-1])
                self._send_json(route_payload(route_id, self.registry))
                return
            self._serve_static(parsed.path)
        except ApiError as error:
            self._send_error(error)
        except RouteRegistryError as error:
            self._send_error(
                ApiError(
                    HTTPStatus.BAD_REQUEST,
                    str(error),
                    code="route_registry_invalid",
                )
            )
        except Exception as error:
            self._send_error(
                ApiError(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "The backend hit an unexpected error.",
                    detail=str(error),
                    code="internal_error",
                )
            )

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/device/select":
                payload = self._read_json_body()
                device_id = payload.get("deviceId") if isinstance(payload, dict) else None
                self._send_json(self.manager.select_device(device_id))
                return
            if parsed.path == "/api/routes/from-google-maps-link":
                generated = generate_google_maps_directions_gpx(
                    self._read_json_body(),
                    imports_directory=self.imports_directory,
                    directions_provider=self.directions_provider,
                    link_expander=self.google_maps_link_expander,
                )
                self._send_json(generated, status=HTTPStatus.CREATED)
                return
            if parsed.path == "/api/schedule/activate":
                controller = self._require_schedule_controller()
                if self.manager.status().get("state") != "idle":
                    raise ApiError(
                        HTTPStatus.CONFLICT,
                        "Stop route playback before activating a location schedule",
                        code="playback_active",
                    )
                try:
                    scheduled = controller.save_and_start(self._read_json_body())
                except ScheduleValidationError as error:
                    raise ApiError(
                        HTTPStatus.BAD_REQUEST,
                        str(error),
                        code="invalid_schedule",
                    ) from error
                except ScheduleStorageError as error:
                    raise ApiError(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        str(error),
                        code="schedule_write_failed",
                    ) from error
                self._send_json(scheduled)
                return
            if parsed.path == "/api/schedule/save":
                controller = self._require_schedule_controller()
                try:
                    saved = controller.save(self._read_json_body())
                except ScheduleValidationError as error:
                    raise ApiError(
                        HTTPStatus.BAD_REQUEST,
                        str(error),
                        code="invalid_schedule",
                    ) from error
                except ScheduleStorageError as error:
                    raise ApiError(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        str(error),
                        code="schedule_write_failed",
                    ) from error
                self._send_json(saved)
                return
            if parsed.path == "/api/schedule/stop":
                controller = self._require_schedule_controller()
                self._send_json(controller.stop(clear_location=True))
                return
            if (
                parsed.path.startswith("/api/schedules/")
                and parsed.path.endswith("/activate")
            ):
                controller = self._require_schedule_controller()
                if self.manager.status().get("state") != "idle":
                    raise ApiError(
                        HTTPStatus.CONFLICT,
                        "Stop route playback before activating a location schedule",
                        code="playback_active",
                    )
                schedule_id = unquote(
                    parsed.path.removeprefix("/api/schedules/").removesuffix(
                        "/activate"
                    )
                ).strip("/")
                try:
                    self._send_json(controller.activate_saved(schedule_id))
                except ScheduleValidationError as error:
                    raise ApiError(
                        HTTPStatus.BAD_REQUEST,
                        str(error),
                        code="invalid_schedule",
                    ) from error
                return
            if parsed.path == "/api/routes/from-directions":
                generated = generate_directions_gpx(
                    self._read_json_body(),
                    imports_directory=self.imports_directory,
                    directions_provider=self.directions_provider,
                )
                self._send_json(generated, status=HTTPStatus.CREATED)
                return
            if parsed.path == "/api/routes/import-gpx":
                imported = import_gpx_payload(
                    self._read_json_body(),
                    imports_directory=self.imports_directory,
                )
                self._send_json(imported, status=HTTPStatus.CREATED)
                return
            if (
                parsed.path.startswith("/api/routes/imports/")
                and parsed.path.endswith("/prepare")
            ):
                filename = unquote(
                    parsed.path.removeprefix("/api/routes/imports/").removesuffix(
                        "/prepare"
                    )
                )
                prepared = prepare_imported_gpx(
                    filename,
                    self._read_json_body(),
                    imports_directory=self.imports_directory,
                    generated_directory=self.generated_directory,
                    registry=self.registry,
                    timing_provider=self.timing_provider,
                )
                self._send_json(prepared)
                return
            if parsed.path.startswith("/api/routes/") and parsed.path.endswith("/start"):
                route_id = unquote(parsed.path.split("/")[-2])
                self._stop_schedule_for_manual_control()
                self._send_json(self.manager.start(route_id))
                return
            if parsed.path == "/api/playback/stop":
                self._send_json(self.manager.stop(clear_location=True))
                return
            if parsed.path == "/api/playback/pause":
                self._send_json(self.manager.pause())
                return
            if parsed.path == "/api/playback/resume":
                self._send_json(self.manager.resume())
                return
            if parsed.path == "/api/location/clear":
                self._stop_schedule_for_manual_control()
                self.manager.stop(clear_location=False)
                self._send_json(self.manager.clear_location())
                return
            if parsed.path == "/api/location/set":
                self._stop_schedule_for_manual_control()
                latitude, longitude = static_location_coordinates(
                    self._read_json_body()
                )
                self._send_json(
                    self.manager.set_location(latitude, longitude)
                )
                return
            raise ApiError(HTTPStatus.NOT_FOUND, f"Unknown endpoint: {parsed.path}")
        except ApiError as error:
            self._send_error(error)
        except Exception as error:
            self._send_error(
                ApiError(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "The backend hit an unexpected error.",
                    detail=str(error),
                    code="internal_error",
                )
            )

    def do_DELETE(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/schedules/"):
                schedule_id = unquote(
                    parsed.path.removeprefix("/api/schedules/")
                )
                try:
                    deleted = self._require_schedule_controller().delete_saved(
                        schedule_id
                    )
                except ScheduleValidationError as error:
                    status = (
                        HTTPStatus.CONFLICT
                        if "active schedule" in str(error).lower()
                        else HTTPStatus.BAD_REQUEST
                    )
                    raise ApiError(
                        status,
                        str(error),
                        code="schedule_delete_failed",
                    ) from error
                self._send_json(deleted)
                return
            if parsed.path.startswith("/api/routes/imports/"):
                filename = unquote(
                    parsed.path.removeprefix("/api/routes/imports/")
                )
                playback = self.manager.status()
                deleted = delete_imported_gpx(
                    filename,
                    imports_directory=self.imports_directory,
                    generated_directory=self.generated_directory,
                    registry=self.registry,
                    active_route_id=playback.get("routeId"),
                )
                self._send_json(deleted)
                return
            raise ApiError(
                HTTPStatus.NOT_FOUND,
                f"Unknown endpoint: {parsed.path}",
            )
        except ApiError as error:
            self._send_error(error)
        except RouteRegistryError as error:
            self._send_error(
                ApiError(
                    HTTPStatus.BAD_REQUEST,
                    str(error),
                    code="route_registry_invalid",
                )
            )
        except Exception as error:
            self._send_error(
                ApiError(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "The backend hit an unexpected error.",
                    detail=str(error),
                    code="internal_error",
                )
            )

    def _read_json_body(self) -> Any:
        if self.headers.get_content_type() != "application/json":
            raise ApiError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "Content-Type must be application/json",
                code="unsupported_media_type",
            )
        raw_length = self.headers.get("Content-Length", "0")
        try:
            content_length = int(raw_length)
        except ValueError as error:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "Content-Length must be a valid integer",
                code="invalid_content_length",
            ) from error
        if content_length < 0:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "Content-Length cannot be negative",
                code="invalid_content_length",
            )
        if content_length > MAX_JSON_REQUEST_BYTES:
            raise ApiError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "JSON request is too large",
                code="request_too_large",
            )

        raw_content = self.rfile.read(content_length)
        try:
            return json.loads(raw_content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "Request body must be valid UTF-8 JSON",
                detail=str(error),
                code="invalid_json",
            ) from error

    def _require_schedule_controller(self) -> LocationScheduleController:
        if self.schedule_controller is None:
            raise ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Location scheduling is not available until the controller is restarted",
                code="schedule_unavailable",
            )
        return self.schedule_controller

    def _stop_schedule_for_manual_control(self) -> None:
        if self.schedule_controller is not None:
            self.schedule_controller.stop(clear_location=False)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (FRONTEND_DIR / relative).resolve()
        if not target.is_relative_to(FRONTEND_DIR.resolve()) or not target.is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, "Not found")

        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        content = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        content = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _send_error(self, error: ApiError) -> None:
        self._send_json(
            {
                "error": error.message,
                "errorCode": error.code,
                "errorDetail": error.detail,
            },
            status=error.status,
        )


def route_payloads(registry: Optional[RouteRegistry] = None) -> list[dict[str, Any]]:
    active_registry = registry or DEFAULT_REGISTRY
    return [route_payload(route.id, active_registry) for route in active_registry.all()]


def _disabled_schedule_status() -> dict[str, Any]:
    return {
        "state": "disabled",
        "enabled": False,
        "scheduleId": None,
        "activeScheduleId": None,
        "schedule": None,
        "schedules": [],
        "activeWindow": None,
        "nextWindow": None,
        "nextTransitionAt": None,
        "lastAppliedAt": None,
        "preventingIdleSleep": False,
    }


def static_location_coordinates(payload: Any) -> tuple[float, float]:
    if not isinstance(payload, dict):
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "Request body must be a JSON object",
            code="invalid_static_location",
        )
    latitude = payload.get("latitude")
    longitude = payload.get("longitude")
    if (
        isinstance(latitude, bool)
        or not isinstance(latitude, (int, float))
        or isinstance(longitude, bool)
        or not isinstance(longitude, (int, float))
    ):
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "Latitude and longitude must be numbers",
            code="invalid_static_location",
        )
    latitude = float(latitude)
    longitude = float(longitude)
    if not -90 <= latitude <= 90:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "Latitude must be between -90 and 90",
            code="invalid_static_location",
        )
    if not -180 <= longitude <= 180:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "Longitude must be between -180 and 180",
            code="invalid_static_location",
        )
    return latitude, longitude


def route_payload(
    route_id: str, registry: Optional[RouteRegistry] = None
) -> dict[str, Any]:
    active_registry = registry or DEFAULT_REGISTRY
    try:
        route = active_registry.get(route_id)
    except RouteRegistryError:
        raise ApiError(HTTPStatus.NOT_FOUND, f"Unknown route: {route_id}")
    name, points = parse_track(route.track_path)
    summary = summarize(name, points)
    source_path = _relative_path(route.source_path) if route.source_path else None
    return {
        "id": route.id,
        "label": route.label,
        "direction": route.direction,
        "originLabel": route.origin_label,
        "destinationLabel": route.destination_label,
        "trackPath": _relative_path(route.track_path),
        "sourcePath": source_path,
        "createdAt": route.created_at,
        "bundled": route.bundled,
        "timingMode": route.timing_mode,
        "timingProvider": route.timing_provider,
        "estimatedDurationSeconds": route.estimated_duration_seconds,
        "timingWarning": route.timing_warning,
        "pointCount": summary.point_count,
        "durationSeconds": summary.duration_seconds,
        "start": {
            "latitude": summary.start.latitude,
            "longitude": summary.start.longitude,
            "time": summary.started_at,
        },
        "end": {
            "latitude": summary.end.latitude,
            "longitude": summary.end.longitude,
            "time": summary.ended_at,
        },
    }


def route_preview_payload(
    route_id: str,
    registry: Optional[RouteRegistry] = None,
) -> dict[str, Any]:
    active_registry = registry or DEFAULT_REGISTRY
    try:
        route = active_registry.get(route_id)
    except RouteRegistryError:
        raise ApiError(HTTPStatus.NOT_FOUND, f"Unknown route: {route_id}")
    _, points = parse_track(route.track_path)
    if len(points) > MAX_ROUTE_PREVIEW_POINTS:
        step = (len(points) - 1) / (MAX_ROUTE_PREVIEW_POINTS - 1)
        indices = sorted(
            {
                round(index * step)
                for index in range(MAX_ROUTE_PREVIEW_POINTS)
            }
        )
        points = [points[index] for index in indices]
    return {
        "routeId": route.id,
        "points": [
            {
                "latitude": point.latitude,
                "longitude": point.longitude,
                "time": point.time,
                "name": point.name,
            }
            for point in points
        ],
    }


def import_gpx_payload(
    payload: Any, *, imports_directory: Path = IMPORTS_DIR
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "Request body must be a JSON object",
            code="invalid_import_request",
        )

    filename = payload.get("filename")
    content = payload.get("content")
    if not isinstance(filename, str) or not filename.strip():
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "'filename' must be a non-empty string",
            code="invalid_import_request",
        )
    if not isinstance(content, str):
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "'content' must be a string",
            code="invalid_import_request",
        )

    original_filename = filename.strip()
    safe_filename = _safe_import_filename(original_filename)
    content_bytes = content.encode("utf-8")
    if len(content_bytes) > MAX_GPX_CONTENT_BYTES:
        raise ApiError(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            "GPX content is larger than 5 MiB",
            code="gpx_too_large",
        )

    try:
        summary = inspect_gpx_content(
            content,
            fallback_name=Path(original_filename).stem,
        )
    except GpxValidationError as error:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            str(error),
            code="invalid_gpx",
        ) from error

    imports_directory.mkdir(parents=True, exist_ok=True)
    stored_path = _write_unique_import(
        imports_directory,
        safe_filename,
        content,
    )
    try:
        _write_import_metadata(
            stored_path,
            {
                "sourceType": "upload",
                "originalFilename": original_filename,
            },
        )
    except ApiError:
        stored_path.unlink(missing_ok=True)
        raise
    return _imported_gpx_payload(
        stored_path,
        summary,
        original_filename=original_filename,
    )


def generate_directions_gpx(
    payload: Any,
    *,
    imports_directory: Path = IMPORTS_DIR,
    directions_provider: Optional[
        DirectionsProvider
    ] = DEFAULT_DIRECTIONS_PROVIDER,
    source_metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "Request body must be a JSON object",
            code="invalid_directions_request",
        )
    profile = payload.get("profile", "driving")
    if profile != "driving":
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "Only the driving route profile is supported",
            code="invalid_directions_request",
        )

    origin = _directions_coordinate(payload.get("origin"), "origin")
    destination = _directions_coordinate(
        payload.get("destination"),
        "destination",
    )
    origin_label = _directions_label(
        payload.get("originLabel"),
        "Origin",
        "originLabel",
    )
    destination_label = _directions_label(
        payload.get("destinationLabel"),
        "Destination",
        "destinationLabel",
    )
    default_name = f"{origin_label} to {destination_label}"
    name = _directions_label(payload.get("name"), default_name, "name", maximum=120)

    if directions_provider is None:
        raise ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "No directions provider is configured",
            code="directions_unavailable",
        )
    try:
        generated = generate_route(
            origin,
            destination,
            provider=directions_provider,
        )
    except DirectionsValidationError as error:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            str(error),
            code="invalid_directions_request",
        ) from error
    except DirectionsProviderError as error:
        raise ApiError(
            HTTPStatus.BAD_GATEWAY,
            str(error),
            code="directions_unavailable",
        ) from error

    started_at = datetime.now(timezone.utc).replace(microsecond=0)
    duration_scale = (
        generated.estimated_duration_seconds
        / sum(generated.segment_durations_seconds)
    )
    offsets = [0.0]
    for duration in generated.segment_durations_seconds:
        offsets.append(offsets[-1] + duration * duration_scale)
    offsets[-1] = generated.estimated_duration_seconds
    points = [
        RoutePoint(
            latitude=point.latitude,
            longitude=point.longitude,
            time=started_at + timedelta(seconds=offsets[index]),
            name=(
                origin_label
                if index == 0
                else destination_label
                if index == len(generated.points) - 1
                else None
            ),
        )
        for index, point in enumerate(generated.points)
    ]
    try:
        content = track_xml(name, points)
    except GpxValidationError as error:
        raise ApiError(
            HTTPStatus.BAD_GATEWAY,
            "The directions response could not be converted to GPX",
            detail=str(error),
            code="directions_invalid_geometry",
        ) from error
    if len(content.encode("utf-8")) > MAX_GPX_CONTENT_BYTES:
        raise ApiError(
            HTTPStatus.BAD_GATEWAY,
            "The generated route is too large to store safely",
            code="directions_route_too_large",
        )

    safe_filename = _safe_import_filename(f"{name}.gpx")
    imports_directory.mkdir(parents=True, exist_ok=True)
    stored_path = _write_unique_import(
        imports_directory,
        safe_filename,
        content,
    )
    import_metadata = {
        "sourceType": "directions",
        "provider": generated.provider,
        "distanceMeters": generated.distance_meters,
        "estimatedDurationSeconds": generated.estimated_duration_seconds,
        "originLabel": origin_label,
        "destinationLabel": destination_label,
        "requestedOrigin": {
            "latitude": origin.latitude,
            "longitude": origin.longitude,
        },
        "requestedDestination": {
            "latitude": destination.latitude,
            "longitude": destination.longitude,
        },
        "originalFilename": stored_path.name,
    }
    if source_metadata:
        import_metadata.update(source_metadata)
    try:
        _write_import_metadata(stored_path, import_metadata)
    except ApiError:
        stored_path.unlink(missing_ok=True)
        raise
    summary = inspect_gpx_content(content, fallback_name=name)
    response = _imported_gpx_payload(
        stored_path,
        summary,
        original_filename=stored_path.name,
    )
    response.update(
        {
            "previewPoints": _preview_points_payload(points),
        }
    )
    return response


def generate_google_maps_directions_gpx(
    payload: Any,
    *,
    imports_directory: Path = IMPORTS_DIR,
    directions_provider: Optional[
        DirectionsProvider
    ] = DEFAULT_DIRECTIONS_PROVIDER,
    link_expander: Optional[GoogleMapsLinkExpander] = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "Request body must be a JSON object",
            code="invalid_google_maps_link",
        )
    value = payload.get("url")
    if not isinstance(value, str):
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "'url' must be a Google Maps directions URL",
            code="invalid_google_maps_link",
        )
    name = _directions_label(
        payload.get("name"),
        "Google Maps route",
        "name",
        maximum=120,
    )
    try:
        link = resolve_google_maps_directions_link(
            value,
            expander=link_expander,
        )
    except GoogleMapsGeocodingRequiredError as error:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            str(error),
            code="google_maps_geocoding_required",
        ) from error
    except GoogleMapsLinkValidationError as error:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            str(error),
            code="invalid_google_maps_link",
        ) from error
    except GoogleMapsLinkResolutionError as error:
        raise ApiError(
            HTTPStatus.BAD_GATEWAY,
            str(error),
            code="google_maps_link_unavailable",
        ) from error

    response = generate_directions_gpx(
        {
            "name": name,
            "origin": {
                "latitude": link.origin.latitude,
                "longitude": link.origin.longitude,
            },
            "destination": {
                "latitude": link.destination.latitude,
                "longitude": link.destination.longitude,
            },
            "originLabel": "Google Maps origin",
            "destinationLabel": "Google Maps destination",
            "profile": "driving",
        },
        imports_directory=imports_directory,
        directions_provider=directions_provider,
        source_metadata={
            "sourceType": "google-maps",
            "sourceUrlHost": link.resolved_host,
            "sourceWasShortened": link.was_shortened,
        },
    )
    return response


def imported_gpx_payloads(imports_directory: Path = IMPORTS_DIR) -> list[dict[str, Any]]:
    if not imports_directory.is_dir():
        return []

    imported: list[dict[str, Any]] = []
    stored_paths = sorted(
        imports_directory.glob("*.gpx"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for stored_path in stored_paths:
        try:
            content = stored_path.read_text(encoding="utf-8")
            summary = inspect_gpx_content(content, fallback_name=stored_path.stem)
        except (OSError, UnicodeDecodeError, GpxValidationError):
            continue
        imported.append(
            _imported_gpx_payload(
                stored_path,
                summary,
                original_filename=stored_path.name,
            )
        )
    return imported


def imported_gpx_detail(
    filename: str, *, imports_directory: Path = IMPORTS_DIR
) -> dict[str, Any]:
    safe_filename = _safe_import_filename(filename)
    if safe_filename != filename:
        raise ApiError(
            HTTPStatus.NOT_FOUND,
            "Imported GPX file was not found",
            code="import_not_found",
        )
    stored_path = imports_directory / safe_filename
    if not stored_path.is_file():
        raise ApiError(
            HTTPStatus.NOT_FOUND,
            "Imported GPX file was not found",
            code="import_not_found",
        )
    try:
        content = stored_path.read_text(encoding="utf-8")
        summary = inspect_gpx_content(content, fallback_name=stored_path.stem)
    except (OSError, UnicodeDecodeError, GpxValidationError) as error:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "The saved GPX import is no longer valid",
            detail=str(error),
            code="invalid_gpx",
        ) from error
    payload = _imported_gpx_payload(
        stored_path,
        summary,
        original_filename=stored_path.name,
    )
    payload["content"] = content
    return payload


def delete_imported_gpx(
    filename: str,
    *,
    imports_directory: Path = IMPORTS_DIR,
    generated_directory: Path = GENERATED_DIR,
    registry: RouteRegistry = DEFAULT_REGISTRY,
    active_route_id: Optional[str] = None,
) -> dict[str, Any]:
    safe_filename = _safe_import_filename(filename)
    if safe_filename != filename:
        raise ApiError(
            HTTPStatus.NOT_FOUND,
            "Imported GPX file was not found",
            code="import_not_found",
        )
    source_path = imports_directory / safe_filename
    if not source_path.is_file():
        raise ApiError(
            HTTPStatus.NOT_FOUND,
            "Imported GPX file was not found",
            code="import_not_found",
        )

    source_resolved = source_path.resolve()
    matching_routes = [
        route
        for route in registry.all()
        if (
            not route.bundled
            and route.source_path is not None
            and route.source_path.resolve() == source_resolved
        )
    ]
    matching_ids = {route.id for route in matching_routes}
    if active_route_id in matching_ids:
        raise ApiError(
            HTTPStatus.CONFLICT,
            "Stop device playback before deleting this imported route",
            code="route_in_use",
        )

    removed_route_ids: list[str] = []
    removed_track_paths: list[str] = []
    generated_root = generated_directory.resolve()
    try:
        for route in matching_routes:
            removed = registry.remove(route.id)
            if removed is None:
                continue
            removed_route_ids.append(removed.id)
            track_path = removed.track_path.resolve()
            if (
                track_path.is_relative_to(generated_root)
                and track_path.is_file()
            ):
                track_path.unlink()
                removed_track_paths.append(_relative_path(track_path))
        source_path.unlink()
        metadata_path = _import_metadata_path(source_path)
        if metadata_path.is_file():
            metadata_path.unlink()
    except OSError as error:
        raise ApiError(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "The imported route could not be completely deleted",
            detail=str(error),
            code="delete_failed",
        ) from error

    return {
        "deleted": True,
        "filename": safe_filename,
        "removedRouteIds": removed_route_ids,
        "removedTrackPaths": removed_track_paths,
    }


def prepare_imported_gpx(
    filename: str,
    payload: Any,
    *,
    imports_directory: Path = IMPORTS_DIR,
    generated_directory: Path = GENERATED_DIR,
    registry: RouteRegistry = DEFAULT_REGISTRY,
    timing_provider: Optional[RoadTimingProvider] = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "Request body must be a JSON object",
            code="invalid_prepare_request",
        )
    duration_value = payload.get("durationSeconds")
    duration_seconds: Optional[float]
    if duration_value is None:
        duration_seconds = None
    else:
        if (
            isinstance(duration_value, bool)
            or not isinstance(duration_value, (int, float))
        ):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "'durationSeconds' must be a number or null",
                code="invalid_prepare_request",
            )
        duration_seconds = float(duration_value)
        if not 10 <= duration_seconds <= 86_400:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "'durationSeconds' must be between 10 and 86400",
                code="invalid_prepare_request",
            )

    timing_mode = payload.get("timingMode", "auto")
    if timing_mode not in {"auto", "source", "route-aware", "uniform"}:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "'timingMode' must be auto, source, route-aware, or uniform",
            code="invalid_prepare_request",
        )

    safe_filename = _safe_import_filename(filename)
    if safe_filename != filename:
        raise ApiError(
            HTTPStatus.NOT_FOUND,
            "Imported GPX file was not found",
            code="import_not_found",
        )
    source_path = imports_directory / safe_filename
    if not source_path.is_file():
        raise ApiError(
            HTTPStatus.NOT_FOUND,
            "Imported GPX file was not found",
            code="import_not_found",
        )
    try:
        content = source_path.read_text(encoding="utf-8")
        source_summary = inspect_gpx_content(
            content,
            fallback_name=source_path.stem,
        )
    except (OSError, UnicodeDecodeError, GpxValidationError) as error:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "The saved GPX import could not be read",
            detail=str(error),
            code="invalid_gpx",
        ) from error

    label_value = payload.get("label")
    if label_value is not None and (
        not isinstance(label_value, str) or not label_value.strip()
    ):
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "'label' must be a non-empty string when provided",
            code="invalid_prepare_request",
        )
    if isinstance(label_value, str) and len(label_value.strip()) > 120:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "'label' must be 120 characters or fewer",
            code="invalid_prepare_request",
        )
    origin_label = _directions_label(
        payload.get("originLabel"),
        source_summary.start_name or "START",
        "originLabel",
    )
    destination_label = _directions_label(
        payload.get("destinationLabel"),
        source_summary.end_name or "END",
        "destinationLabel",
    )

    try:
        source_metadata = _read_import_metadata(source_path)
        prepared = prepare_gpx_playback_result(
            content,
            fallback_name=source_path.stem,
            duration_seconds=duration_seconds,
            interpolate_seconds=0.5,
            start_time=datetime.now(timezone.utc).replace(microsecond=0),
            timing_mode=timing_mode,
            timing_provider=timing_provider,
        )
        route_name = prepared.name
        points = list(prepared.points)
        label = label_value.strip() if isinstance(label_value, str) else route_name
        generated_directory.mkdir(parents=True, exist_ok=True)
        track_path = generated_directory / f"{source_path.stem}.track.gpx"
        write_track(track_path, label, points)

        route = registry.upsert(
            RouteRecord(
                id=f"generated-{source_path.stem.lower()}",
                label=label,
                direction="custom",
                origin_label=origin_label,
                destination_label=destination_label,
                track_path=track_path,
                source_path=source_path,
                created_at=datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                bundled=False,
                timing_mode=prepared.timing.mode,
                timing_provider=prepared.timing.provider,
                estimated_duration_seconds=(
                    prepared.timing.estimated_duration_seconds
                ),
                timing_warning=prepared.timing.warning,
            )
        )
    except (GpxValidationError, RouteRegistryError, OSError) as error:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            str(error),
            code="route_prepare_failed",
        ) from error
    response = route_payload(route.id, registry)
    response["previewPoints"] = [
        {
            "latitude": point.latitude,
            "longitude": point.longitude,
            "time": point.time,
            "name": point.name,
        }
        for point in prepared.preview_points
    ]
    return response


def _imported_gpx_payload(
    stored_path: Path,
    summary: Any,
    *,
    original_filename: str,
) -> dict[str, Any]:
    minimum_latitude, minimum_longitude, maximum_latitude, maximum_longitude = summary.bounds
    created_at = datetime.fromtimestamp(
        stored_path.stat().st_mtime,
        timezone.utc,
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    payload = {
        "id": f"import-{stored_path.stem.lower()}",
        "name": summary.name,
        "filename": stored_path.name,
        "originalFilename": original_filename,
        "storedPath": _relative_path(stored_path),
        "createdAt": created_at,
        "geometryType": summary.geometry_type,
        "segmentCount": summary.segment_count,
        "pointCount": summary.point_count,
        "timestampedPointCount": summary.timestamped_point_count,
        "hasTimestamps": summary.timestamped_point_count == summary.point_count,
        "durationSeconds": summary.duration_seconds,
        "originLabel": summary.start_name or "START",
        "destinationLabel": summary.end_name or "END",
        "start": {
            "latitude": summary.start[0],
            "longitude": summary.start[1],
        },
        "end": {
            "latitude": summary.end[0],
            "longitude": summary.end[1],
        },
        "bounds": {
            "south": minimum_latitude,
            "west": minimum_longitude,
            "north": maximum_latitude,
            "east": maximum_longitude,
        },
    }
    payload.update(_read_import_metadata(stored_path))
    return payload


def _directions_coordinate(value: Any, field: str) -> DirectionsCoordinate:
    if not isinstance(value, dict):
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"'{field}' must be an object with latitude and longitude",
            code="invalid_directions_request",
        )
    latitude = value.get("latitude")
    longitude = value.get("longitude")
    if (
        isinstance(latitude, bool)
        or not isinstance(latitude, (int, float))
        or isinstance(longitude, bool)
        or not isinstance(longitude, (int, float))
    ):
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"'{field}' latitude and longitude must be numbers",
            code="invalid_directions_request",
        )
    return DirectionsCoordinate(
        latitude=float(latitude),
        longitude=float(longitude),
    )


def _directions_label(
    value: Any,
    default: str,
    field: str,
    *,
    maximum: int = 80,
) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"'{field}' must be a non-empty string when provided",
            code="invalid_directions_request",
        )
    label = value.strip()
    if len(label) > maximum:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"'{field}' must be {maximum} characters or fewer",
            code="invalid_directions_request",
        )
    return label


def _preview_points_payload(
    points: list[RoutePoint],
) -> list[dict[str, Any]]:
    preview_points = points
    if len(points) > MAX_ROUTE_PREVIEW_POINTS:
        step = (len(points) - 1) / (MAX_ROUTE_PREVIEW_POINTS - 1)
        indices = sorted(
            {
                round(index * step)
                for index in range(MAX_ROUTE_PREVIEW_POINTS)
            }
        )
        preview_points = [points[index] for index in indices]
    return [
        {
            "latitude": point.latitude,
            "longitude": point.longitude,
            "time": point.time,
            "name": point.name,
        }
        for point in preview_points
    ]


def _safe_import_filename(filename: str) -> str:
    if len(filename) > 255:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "'filename' is too long",
            code="invalid_import_request",
        )
    if filename != Path(filename).name or "/" in filename or "\\" in filename:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "'filename' must not contain a directory path",
            code="invalid_import_request",
        )
    path = Path(filename)
    if path.suffix.lower() != ".gpx":
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "'filename' must end in .gpx",
            code="invalid_import_request",
        )

    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", path.stem).strip("._-")
    if not stem:
        stem = "imported-route"
    return f"{stem}.gpx"


def _write_unique_import(directory: Path, filename: str, content: str) -> Path:
    requested = Path(filename)
    for suffix in range(1, 10_000):
        candidate_name = (
            requested.name
            if suffix == 1
            else f"{requested.stem}-{suffix}{requested.suffix}"
        )
        candidate = directory / candidate_name
        try:
            with candidate.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            return candidate
        except FileExistsError:
            continue
    raise ApiError(
        HTTPStatus.CONFLICT,
        "Could not allocate a unique filename for this import",
        code="import_name_conflict",
    )


_IMPORT_METADATA_FIELDS = {
    "distanceMeters",
    "destinationLabel",
    "estimatedDurationSeconds",
    "originalFilename",
    "originLabel",
    "provider",
    "requestedDestination",
    "requestedOrigin",
    "sourceType",
    "sourceUrlHost",
    "sourceWasShortened",
}


def _import_metadata_path(stored_path: Path) -> Path:
    return stored_path.with_suffix(f"{stored_path.suffix}.metadata.json")


def _write_import_metadata(
    stored_path: Path,
    metadata: dict[str, Any],
) -> None:
    filtered = {
        key: value
        for key, value in metadata.items()
        if key in _IMPORT_METADATA_FIELDS
    }
    metadata_path = _import_metadata_path(stored_path)
    temporary_path = metadata_path.with_name(
        f".{metadata_path.name}.{time.time_ns()}.tmp"
    )
    try:
        temporary_path.write_text(
            json.dumps(filtered, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, metadata_path)
    except OSError as error:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ApiError(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "Route metadata could not be saved",
            detail=str(error),
            code="metadata_write_failed",
        ) from error


def _read_import_metadata(stored_path: Path) -> dict[str, Any]:
    metadata_path = _import_metadata_path(stored_path)
    try:
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        key: value
        for key, value in raw.items()
        if key in _IMPORT_METADATA_FIELDS
    }


def _relative_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    if host not in ("127.0.0.1", "localhost"):
        raise ApiError(HTTPStatus.BAD_REQUEST, "The backend must bind to loopback only")

    handler = RouteRequestHandler
    handler.registry = DEFAULT_REGISTRY
    handler.manager = PlaybackManager(userspace=True, registry=DEFAULT_REGISTRY)
    handler.schedule_controller = LocationScheduleController(
        SCHEDULE_PATH,
        set_location=handler.manager.set_location,
        clear_location=handler.manager.clear_location,
    )
    server = ThreadingHTTPServer((host, port), handler)

    def stop_server(signum: int, frame: Any) -> None:
        Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    print(f"Route Controller backend: http://{host}:{port}/")
    try:
        server.serve_forever()
    finally:
        handler.schedule_controller.shutdown()
        handler.manager.stop(clear_location=True)
