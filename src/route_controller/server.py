from __future__ import annotations

import json
import mimetypes
import signal
import subprocess
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from .environment import inspect_environment
from .gpx import parse_track, summarize
from .playback import clear_arguments, play_arguments, resolve_executable


ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = ROOT / "frontend"
ROUTES = {
    "l1-to-l2": {
        "id": "l1-to-l2",
        "label": "L1 to L2",
        "direction": "outbound",
        "path": ROOT / "routes/tracks/route_L1_to_L2.track.gpx",
    },
    "l2-to-l1": {
        "id": "l2-to-l1",
        "label": "L2 to L1",
        "direction": "inbound",
        "path": ROOT / "routes/tracks/route_L2_to_L1.track.gpx",
    },
}


@dataclass
class ActivePlayback:
    route_id: str
    label: str
    started_at: float
    duration_seconds: float
    process: subprocess.Popen[str]
    paused_at: Optional[float] = None
    total_paused_seconds: float = 0.0


class PlaybackManager:
    def __init__(self, *, userspace: bool = True) -> None:
        self._lock = Lock()
        self._active: Optional[ActivePlayback] = None
        self._userspace = userspace

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._reap_locked()
            return self._status_locked()

    def start(self, route_id: str) -> dict[str, Any]:
        route = ROUTES.get(route_id)
        if route is None:
            raise ApiError(HTTPStatus.NOT_FOUND, f"Unknown route: {route_id}")

        with self._lock:
            self._reap_locked()
            if self._active is not None:
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    f"{self._active.label} is already playing",
                )

            command_executable = resolve_executable(None)
            arguments = play_arguments(
                command_executable,
                route["path"],
                userspace=self._userspace,
            )
            name, points = parse_track(route["path"])
            summary = summarize(name, points)
            process = subprocess.Popen(
                arguments,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            self._active = ActivePlayback(
                route_id=route_id,
                label=str(route["label"]),
                started_at=time.monotonic(),
                duration_seconds=summary.duration_seconds,
                process=process,
            )
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
            active = self._active
            if active is not None:
                active.process.terminate()
                try:
                    active.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    active.process.kill()
                    active.process.wait(timeout=5)
                self._active = None
                stopped_active_process = True

        if clear_location and stopped_active_process:
            self.clear_location()
        return self.status()

    def clear_location(self) -> dict[str, Any]:
        command_executable = resolve_executable(None)
        result = subprocess.run(
            clear_arguments(command_executable, userspace=self._userspace),
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "clear failed"
            raise ApiError(HTTPStatus.BAD_GATEWAY, message)
        return {"state": "cleared"}

    def _reap_locked(self) -> None:
        if self._active is None:
            return
        returncode = self._active.process.poll()
        if returncode is not None:
            self._active = None

    def _status_locked(self) -> dict[str, Any]:
        if self._active is None:
            return {"state": "idle"}

        now = time.monotonic()
        paused_seconds = self._active.total_paused_seconds
        if self._active.paused_at is not None:
            paused_seconds += now - self._active.paused_at
        elapsed = max(0.0, now - self._active.started_at - paused_seconds)
        progress = min(1.0, elapsed / self._active.duration_seconds)
        return {
            "state": "paused" if self._active.paused_at is not None else "playing",
            "routeId": self._active.route_id,
            "label": self._active.label,
            "startedAt": self._active.started_at,
            "elapsedSeconds": elapsed,
            "durationSeconds": self._active.duration_seconds,
            "progress": progress,
            "pid": self._active.process.pid,
        }


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class RouteRequestHandler(BaseHTTPRequestHandler):
    manager: PlaybackManager

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
            self._send_json({"error": error.message}, status=error.status)

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/status":
                self._send_json(
                    {
                        "device": inspect_environment(probe_device=True).as_dict(),
                        "playback": self.manager.status(),
                    }
                )
                return
            if parsed.path == "/api/routes":
                self._send_json({"routes": route_payloads()})
                return
            if parsed.path.startswith("/api/routes/"):
                route_id = unquote(parsed.path.rsplit("/", 1)[-1])
                self._send_json(route_payload(route_id))
                return
            self._serve_static(parsed.path)
        except ApiError as error:
            self._send_json({"error": error.message}, status=error.status)
        except Exception as error:
            self._send_json({"error": str(error)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/routes/") and parsed.path.endswith("/start"):
                route_id = unquote(parsed.path.split("/")[-2])
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
                self.manager.stop(clear_location=False)
                self._send_json(self.manager.clear_location())
                return
            raise ApiError(HTTPStatus.NOT_FOUND, f"Unknown endpoint: {parsed.path}")
        except ApiError as error:
            self._send_json({"error": error.message}, status=error.status)
        except Exception as error:
            self._send_json({"error": str(error)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

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


def route_payloads() -> list[dict[str, Any]]:
    return [route_payload(route_id) for route_id in ROUTES]


def route_payload(route_id: str) -> dict[str, Any]:
    route = ROUTES.get(route_id)
    if route is None:
        raise ApiError(HTTPStatus.NOT_FOUND, f"Unknown route: {route_id}")
    name, points = parse_track(route["path"])
    summary = summarize(name, points)
    return {
        "id": route["id"],
        "label": route["label"],
        "direction": route["direction"],
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


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    if host not in ("127.0.0.1", "localhost"):
        raise ApiError(HTTPStatus.BAD_REQUEST, "The backend must bind to loopback only")

    handler = RouteRequestHandler
    handler.manager = PlaybackManager(userspace=True)
    server = ThreadingHTTPServer((host, port), handler)

    def stop_server(signum: int, frame: Any) -> None:
        Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    print(f"Route Controller backend: http://{host}:{port}/")
    try:
        server.serve_forever()
    finally:
        handler.manager.stop(clear_location=True)
