import signal
import subprocess
from io import StringIO
from pathlib import Path

import pytest

from route_controller.server import ApiError, PlaybackManager, friendly_device_error, route_payload
from route_controller.routes import RouteRegistry, RouteRegistryError


ROOT = Path(__file__).resolve().parents[1]


class FakeProcess:
    next_pid = 4000

    def __init__(self, arguments, **kwargs):
        self.arguments = arguments
        self.returncode = None
        self.pid = FakeProcess.next_pid
        self.signals = []
        self.terminated = False
        self.killed = False
        self.stderr = StringIO("")
        FakeProcess.next_pid += 1

    def poll(self):
        return self.returncode

    def send_signal(self, signum):
        self.signals.append(signum)

    def terminate(self):
        self.terminated = True
        self.returncode = -signal.SIGTERM

    def kill(self):
        self.killed = True
        self.returncode = -signal.SIGKILL

    def wait(self, timeout=None):
        return self.returncode


def test_route_payload_reports_checked_in_tracks() -> None:
    outbound = route_payload("l1-to-l2")
    inbound = route_payload("l2-to-l1")

    assert outbound["label"] == "L1 to L2"
    assert outbound["originLabel"] == "L1"
    assert outbound["destinationLabel"] == "L2"
    assert outbound["trackPath"] == "routes/tracks/route_L1_to_L2.track.gpx"
    assert outbound["bundled"] is True
    assert outbound["pointCount"] == 2933
    assert inbound["pointCount"] == 2441
    assert outbound["durationSeconds"] == 1200
    assert inbound["durationSeconds"] == 1200


def test_route_registry_loads_seeded_routes() -> None:
    registry = RouteRegistry(ROOT)
    routes = registry.all()

    assert [route.id for route in routes] == ["l1-to-l2", "l2-to-l1"]
    assert routes[0].track_path == ROOT / "routes/tracks/route_L1_to_L2.track.gpx"


def test_route_registry_rejects_paths_outside_project(tmp_path: Path) -> None:
    registry_path = tmp_path / "routes.json"
    registry_path.write_text(
        """
        {
          "routes": [
            {
              "id": "bad",
              "label": "Bad",
              "direction": "outbound",
              "trackPath": "../outside.gpx"
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(ROOT, registry_path=registry_path)

    with pytest.raises(RouteRegistryError, match="escapes project root"):
        registry.all()


def test_playback_manager_owns_one_process(monkeypatch) -> None:
    processes = []

    def fake_popen(arguments, **kwargs):
        process = FakeProcess(arguments, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("route_controller.server.resolve_executable", lambda _: "pymobiledevice3")
    monkeypatch.setattr("route_controller.server.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "route_controller.server.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )

    manager = PlaybackManager(userspace=True, registry=RouteRegistry(ROOT))
    status = manager.start("l1-to-l2")

    assert status["state"] == "playing"
    assert status["routeId"] == "l1-to-l2"
    assert processes[0].arguments[:5] == [
        "pymobiledevice3",
        "developer",
        "dvt",
        "simulate-location",
        "play",
    ]
    assert "--userspace" in processes[0].arguments

    with pytest.raises(ApiError, match="already playing"):
        manager.start("l2-to-l1")

    paused = manager.pause()
    assert paused["state"] == "paused"
    assert processes[0].signals == [signal.SIGSTOP]

    resumed = manager.resume()
    assert resumed["state"] == "playing"
    assert processes[0].signals == [signal.SIGSTOP, signal.SIGCONT]

    stopped = manager.stop(clear_location=True)
    assert stopped["state"] == "idle"
    assert processes[0].terminated is True


def test_friendly_device_error_explains_tunneld_recovery() -> None:
    message = friendly_device_error(
        "ERROR Unable to connect to Tunneld. You can start one using: "
        "sudo python3 -m pymobiledevice3 remote tunneld"
    )

    assert "developer tunnel is not running" in message.lower()
    assert "tunneld" in message


def test_failed_playback_status_keeps_friendly_error(monkeypatch) -> None:
    processes = []

    def fake_popen(arguments, **kwargs):
        process = FakeProcess(arguments, **kwargs)
        process.stderr = StringIO("No USB-connected iPhone found")
        processes.append(process)
        return process

    monkeypatch.setattr("route_controller.server.resolve_executable", lambda _: "pymobiledevice3")
    monkeypatch.setattr("route_controller.server.subprocess.Popen", fake_popen)

    manager = PlaybackManager(userspace=True)
    manager.start("l1-to-l2")
    processes[0].returncode = 1

    status = manager.status()

    assert status["state"] == "idle"
    assert status["error"]["code"] == "playback_failed"
    assert "No USB iPhone was detected" in status["error"]["message"]
