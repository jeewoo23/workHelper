import http.client
import json
import signal
import subprocess
from io import StringIO
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from route_controller.environment import DeviceTarget, EnvironmentReport
from route_controller.server import (
    ApiError,
    PlaybackManager,
    RouteRequestHandler,
    delete_imported_gpx,
    friendly_device_error,
    generate_directions_gpx,
    generate_google_maps_directions_gpx,
    imported_gpx_detail,
    imported_gpx_payloads,
    import_gpx_payload,
    prepare_imported_gpx,
    route_preview_payload,
    route_payload,
    static_location_coordinates,
)
from route_controller.directions import (
    DirectionsCoordinate,
    GeneratedDirections,
)
from route_controller.routes import RouteRecord, RouteRegistry, RouteRegistryError
from route_controller.timing import RoadTimingEstimate


ROOT = Path(__file__).resolve().parents[1]
IPAD = DeviceTarget(
    identifier="00008110-TEST-IPAD",
    name="Test iPad",
    device_class="iPad",
    product_type="iPad14,5",
    product_version="18.6",
    connection_type="USB",
    os_name="iPadOS",
    supported=True,
    compatible=True,
)
IPHONE = DeviceTarget(
    identifier="00008120-TEST-IPHONE",
    name="Test iPhone",
    device_class="iPhone",
    product_type="iPhone17,1",
    product_version="18.6",
    connection_type="USB",
    os_name="iOS",
    supported=True,
    compatible=True,
)


def device_report(*devices: DeviceTarget) -> EnvironmentReport:
    return EnvironmentReport(
        macos_version="15.0",
        python_version="3.13",
        xcode_version="Xcode test",
        pymobiledevice3_path="/bin/pymobiledevice3",
        device_probe_attempted=True,
        device_probe_ok=any(device.compatible for device in devices),
        device_count=len(devices),
        device_probe_output=(
            json.dumps([device.as_dict() for device in devices])
            if devices
            else "No USB-connected iPhone or iPad found"
        ),
        devices=tuple(devices),
    )


def ipad_provider() -> EnvironmentReport:
    return device_report(IPAD)


UNTIMED_TRACK = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="MapsToGPX"
     xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <name>L1 to L2 import</name>
    <trkseg>
      <trkpt lat="37.3835546" lon="-122.1371287"/>
      <trkpt lat="37.41584954048625" lon="-122.03492834466675"/>
    </trkseg>
  </trk>
</gpx>
"""


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


class FakeRoadTimingProvider:
    name = "Test Roads"

    def estimate(self, points):
        segment_count = len(points) - 1
        durations = tuple(
            30.0 + index * 10.0
            for index in range(segment_count)
        )
        return RoadTimingEstimate(
            provider=self.name,
            segment_durations_seconds=durations,
            estimated_duration_seconds=sum(durations),
            anchor_count=len(points),
        )


class FakeDirectionsProvider:
    name = "Test Directions"

    def __init__(self):
        self.calls = []

    def route(self, origin, destination):
        self.calls.append((origin, destination))
        midpoint = DirectionsCoordinate(
            latitude=(origin.latitude + destination.latitude) / 2,
            longitude=(origin.longitude + destination.longitude) / 2,
        )
        return GeneratedDirections(
            provider=self.name,
            points=(origin, midpoint, destination),
            segment_durations_seconds=(20.0, 40.0),
            distance_meters=4200.0,
            estimated_duration_seconds=60.0,
        )


def test_single_ipad_is_auto_selected() -> None:
    manager = PlaybackManager(device_provider=ipad_provider)

    status = manager.device_status()

    assert status["selectionRequired"] is False
    assert status["selectedDeviceId"] == IPAD.identifier
    assert status["selectedDevice"]["deviceClass"] == "iPad"
    assert status["selectedDevice"]["osName"] == "iPadOS"


def test_multiple_devices_require_explicit_selection() -> None:
    manager = PlaybackManager(
        device_provider=lambda: device_report(IPHONE, IPAD)
    )

    status = manager.device_status()

    assert status["selectionRequired"] is True
    assert status["selectedDevice"] is None

    selected = manager.select_device(IPAD.identifier)

    assert selected["selectedDeviceId"] == IPAD.identifier
    assert selected["selectedDevice"]["deviceClass"] == "iPad"


def test_disconnected_selected_ipad_blocks_new_commands() -> None:
    connected = [True]
    manager = PlaybackManager(
        registry=RouteRegistry(ROOT),
        device_provider=lambda: device_report(IPAD) if connected[0] else device_report(),
    )
    assert manager.device_status()["selectedDeviceId"] == IPAD.identifier
    connected[0] = False

    with pytest.raises(ApiError) as raised:
        manager.start("l1-to-l2")

    assert raised.value.code == "device_unavailable"


def test_replacement_iphone_is_not_selected_after_ipad_disconnects() -> None:
    connected = [IPAD]
    manager = PlaybackManager(
        registry=RouteRegistry(ROOT),
        device_provider=lambda: device_report(*connected),
    )
    assert manager.device_status()["selectedDeviceId"] == IPAD.identifier
    connected[:] = [IPHONE]

    status = manager.device_status()

    assert status["selectedDeviceId"] == IPAD.identifier
    assert status["selectedDevice"] is None
    assert status["selectionRequired"] is True
    with pytest.raises(ApiError) as raised:
        manager.start("l1-to-l2")
    assert raised.value.code == "device_selection_required"


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
    assert outbound["timingMode"] == "source"


def test_route_registry_loads_seeded_routes() -> None:
    registry = RouteRegistry(ROOT)
    routes = registry.all()
    routes_by_id = {route.id: route for route in routes}

    assert {"l1-to-l2", "l2-to-l1"} <= routes_by_id.keys()
    assert (
        routes_by_id["l1-to-l2"].track_path
        == ROOT / "routes/tracks/route_L1_to_L2.track.gpx"
    )


def test_route_registry_ignores_stale_generated_tracks(tmp_path: Path) -> None:
    registry_path = tmp_path / "routes.json"
    registry_path.write_text(
        json.dumps(
            {
                "routes": [
                    {
                        "id": "stale-generated",
                        "label": "Stale generated route",
                        "direction": "custom",
                        "trackPath": "routes/generated/missing.track.gpx",
                        "bundled": False,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    registry = RouteRegistry(tmp_path, registry_path=registry_path)

    assert registry.all() == []


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


def test_route_registry_upserts_generated_route(tmp_path: Path) -> None:
    track_path = tmp_path / "routes/generated/custom.track.gpx"
    track_path.parent.mkdir(parents=True)
    track_path.write_text("<gpx/>", encoding="utf-8")
    registry = RouteRegistry(tmp_path)

    registry.upsert(
        RouteRecord(
            id="generated-custom",
            label="Custom route",
            direction="custom",
            track_path=track_path,
        )
    )

    saved = registry.get("generated-custom")
    assert saved.track_path == track_path
    assert saved.label == "Custom route"


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

    manager = PlaybackManager(
        userspace=True,
        registry=RouteRegistry(ROOT),
        device_provider=ipad_provider,
    )
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
    assert processes[0].arguments[6:8] == ["--udid", IPAD.identifier]

    with pytest.raises(ApiError, match="already playing"):
        manager.start("l2-to-l1")
    with pytest.raises(ApiError, match="Stop route playback"):
        manager.set_location(37.3, -122.1)

    paused = manager.pause()
    assert paused["state"] == "paused"
    assert processes[0].signals == [signal.SIGSTOP]

    resumed = manager.resume()
    assert resumed["state"] == "playing"
    assert processes[0].signals == [signal.SIGSTOP, signal.SIGCONT]

    stopped = manager.stop(clear_location=True)
    assert stopped["state"] == "idle"
    assert processes[0].terminated is True


def test_playback_manager_sets_and_clears_static_location(monkeypatch) -> None:
    processes = []

    def fake_popen(arguments, **kwargs):
        process = FakeProcess(arguments, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(
        "route_controller.server.resolve_executable",
        lambda _: "pymobiledevice3",
    )
    monkeypatch.setattr(
        "route_controller.server.subprocess.Popen",
        fake_popen,
    )
    monkeypatch.setattr(
        "route_controller.server.subprocess.run",
        lambda arguments, **kwargs: subprocess.CompletedProcess(
            arguments, 0, "", ""
        ),
    )
    manager = PlaybackManager(userspace=True, device_provider=ipad_provider)

    activated = manager.set_location(37.3835546, -122.1371287)

    assert activated == {
        "state": "active",
        "latitude": 37.3835546,
        "longitude": -122.1371287,
    }
    assert manager.simulated_location() == {
        "latitude": 37.3835546,
        "longitude": -122.1371287,
    }
    assert processes[0].arguments == [
        "pymobiledevice3",
        "developer",
        "dvt",
        "simulate-location",
        "set",
        "--userspace",
        "--udid",
        IPAD.identifier,
        "--",
        "37.3835546",
        "-122.1371287",
    ]

    assert manager.clear_location() == {"state": "cleared"}
    assert processes[0].terminated is True
    assert manager.simulated_location() is None


def test_static_location_coordinates_require_valid_numbers() -> None:
    assert static_location_coordinates(
        {"latitude": 37.3, "longitude": -122.1}
    ) == (37.3, -122.1)

    for payload in (
        None,
        {"latitude": True, "longitude": 0},
        {"latitude": 91, "longitude": 0},
        {"latitude": 0, "longitude": -181},
    ):
        with pytest.raises(ApiError) as raised:
            static_location_coordinates(payload)
        assert raised.value.code == "invalid_static_location"


def test_static_location_endpoint_sets_reports_and_clears(
    tmp_path: Path,
    monkeypatch,
) -> None:
    commands = []
    processes = []

    def fake_run(arguments, **kwargs):
        commands.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    def fake_popen(arguments, **kwargs):
        process = FakeProcess(arguments, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(
        "route_controller.server.resolve_executable",
        lambda _: "pymobiledevice3",
    )
    monkeypatch.setattr(
        "route_controller.server.subprocess.Popen",
        fake_popen,
    )
    monkeypatch.setattr(
        "route_controller.server.subprocess.run",
        fake_run,
    )

    class StaticLocationHandler(RouteRequestHandler):
        registry = RouteRegistry(tmp_path)

    StaticLocationHandler.manager = PlaybackManager(
        userspace=True,
        registry=StaticLocationHandler.registry,
        device_provider=ipad_provider,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), StaticLocationHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        server.server_address[1],
        timeout=5,
    )
    try:
        connection.request(
            "POST",
            "/api/location/set",
            body=json.dumps(
                {"latitude": 37.3835546, "longitude": -122.1371287}
            ),
            headers={"Content-Type": "application/json"},
        )
        set_response = connection.getresponse()
        set_payload = json.loads(set_response.read())

        connection.request("GET", "/api/status")
        status_response = connection.getresponse()
        status_payload = json.loads(status_response.read())

        connection.request(
            "POST",
            "/api/location/set",
            body=json.dumps({"latitude": 100, "longitude": 0}),
            headers={"Content-Type": "application/json"},
        )
        invalid_response = connection.getresponse()
        invalid_payload = json.loads(invalid_response.read())

        connection.request("POST", "/api/location/clear")
        clear_response = connection.getresponse()
        clear_payload = json.loads(clear_response.read())
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert set_response.status == 200
    assert set_payload["state"] == "active"
    assert status_response.status == 200
    assert status_payload["simulatedLocation"] == {
        "latitude": 37.3835546,
        "longitude": -122.1371287,
    }
    assert status_payload["capabilities"]["staticLocation"] is True
    assert invalid_response.status == 400
    assert invalid_payload["errorCode"] == "invalid_static_location"
    assert clear_response.status == 200
    assert clear_payload == {"state": "cleared"}
    assert processes[0].arguments[4:9] == [
        "set",
        "--userspace",
        "--udid",
        IPAD.identifier,
        "--",
    ]
    assert processes[0].terminated is True
    assert commands[-1][4:] == [
        "clear",
        "--userspace",
        "--udid",
        IPAD.identifier,
    ]


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

    manager = PlaybackManager(userspace=True, device_provider=ipad_provider)
    manager.start("l1-to-l2")
    processes[0].returncode = 1

    status = manager.status()

    assert status["state"] == "idle"
    assert status["error"]["code"] == "playback_failed"
    assert "No USB iPad was detected" in status["error"]["message"]


def test_import_gpx_payload_saves_untimed_route_and_returns_summary(
    tmp_path: Path,
) -> None:
    payload = {
        "filename": "My MapsToGPX route.gpx",
        "content": UNTIMED_TRACK,
    }

    imported = import_gpx_payload(payload, imports_directory=tmp_path)
    duplicate = import_gpx_payload(payload, imports_directory=tmp_path)

    assert imported["name"] == "L1 to L2 import"
    assert imported["filename"] == "My-MapsToGPX-route.gpx"
    assert imported["geometryType"] == "track"
    assert imported["pointCount"] == 2
    assert imported["timestampedPointCount"] == 0
    assert imported["hasTimestamps"] is False
    assert imported["start"] == {
        "latitude": 37.3835546,
        "longitude": -122.1371287,
    }
    assert imported["end"] == {
        "latitude": 37.41584954048625,
        "longitude": -122.03492834466675,
    }
    assert (tmp_path / imported["filename"]).read_text(encoding="utf-8") == UNTIMED_TRACK
    assert duplicate["filename"] == "My-MapsToGPX-route-2.gpx"
    assert [item["filename"] for item in imported_gpx_payloads(tmp_path)] == [
        "My-MapsToGPX-route-2.gpx",
        "My-MapsToGPX-route.gpx",
    ]
    detail = imported_gpx_detail(imported["filename"], imports_directory=tmp_path)
    assert detail["content"] == UNTIMED_TRACK


def test_import_gpx_payload_rejects_filename_paths(tmp_path: Path) -> None:
    with pytest.raises(ApiError) as raised:
        import_gpx_payload(
            {
                "filename": "../outside.gpx",
                "content": UNTIMED_TRACK,
            },
            imports_directory=tmp_path,
        )

    assert raised.value.status == 400
    assert raised.value.code == "invalid_import_request"
    assert not list(tmp_path.iterdir())


def test_generate_directions_gpx_saves_timed_preview_for_preparation(
    tmp_path: Path,
) -> None:
    provider = FakeDirectionsProvider()
    imports_directory = tmp_path / "routes/imports"

    generated = generate_directions_gpx(
        {
            "name": "L2 to Physical",
            "origin": {
                "latitude": 37.4158495,
                "longitude": -122.0349283,
            },
            "destination": {
                "latitude": 37.3920662,
                "longitude": -122.0947471,
            },
            "originLabel": "Simulated",
            "destinationLabel": "Mac physical",
            "profile": "driving",
        },
        imports_directory=imports_directory,
        directions_provider=provider,
    )

    assert generated["sourceType"] == "directions"
    assert generated["provider"] == "Test Directions"
    assert generated["distanceMeters"] == 4200
    assert generated["estimatedDurationSeconds"] == 60
    assert generated["durationSeconds"] == 60
    assert generated["hasTimestamps"] is True
    assert generated["originLabel"] == "Simulated"
    assert generated["destinationLabel"] == "Mac physical"
    assert generated["requestedOrigin"] == {
        "latitude": 37.4158495,
        "longitude": -122.0349283,
    }
    assert len(generated["previewPoints"]) == 3
    assert (imports_directory / generated["filename"]).is_file()
    assert (
        imports_directory / f"{generated['filename']}.metadata.json"
    ).is_file()
    assert provider.calls[0][0] == DirectionsCoordinate(
        latitude=37.4158495,
        longitude=-122.0349283,
    )
    reloaded = imported_gpx_payloads(imports_directory)[0]
    assert reloaded["sourceType"] == "directions"
    assert reloaded["provider"] == "Test Directions"
    assert reloaded["estimatedDurationSeconds"] == 60
    assert reloaded["originLabel"] == "Simulated"

    registry = RouteRegistry(tmp_path)
    prepared = prepare_imported_gpx(
        generated["filename"],
        {
            "durationSeconds": None,
            "timingMode": "auto",
            "label": "L2 to Physical",
            "originLabel": generated["originLabel"],
            "destinationLabel": generated["destinationLabel"],
        },
        imports_directory=imports_directory,
        generated_directory=tmp_path / "routes/generated",
        registry=registry,
        timing_provider=FakeRoadTimingProvider(),
    )

    assert prepared["timingMode"] == "source"
    assert prepared["durationSeconds"] == 60
    assert prepared["originLabel"] == "Simulated"
    assert prepared["destinationLabel"] == "Mac physical"


def test_directions_endpoint_returns_saved_preview(tmp_path: Path) -> None:
    provider = FakeDirectionsProvider()

    class DirectionsHandler(RouteRequestHandler):
        imports_directory = tmp_path / "routes/imports"
        generated_directory = tmp_path / "routes/generated"
        registry = RouteRegistry(tmp_path)
        timing_provider = FakeRoadTimingProvider()
        directions_provider = provider

    DirectionsHandler.manager = PlaybackManager(registry=DirectionsHandler.registry)
    server = ThreadingHTTPServer(("127.0.0.1", 0), DirectionsHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        server.server_address[1],
        timeout=5,
    )
    try:
        connection.request(
            "POST",
            "/api/routes/from-directions",
            body=json.dumps(
                {
                    "name": "Generated drive",
                    "origin": {
                        "latitude": 37.4158495,
                        "longitude": -122.0349283,
                    },
                    "destination": {
                        "latitude": 37.3920662,
                        "longitude": -122.0947471,
                    },
                    "originLabel": "Simulated",
                    "destinationLabel": "Mac physical",
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())

        connection.request("GET", "/api/routes/imports")
        list_response = connection.getresponse()
        list_payload = json.loads(list_response.read())
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert response.status == 201
    assert payload["name"] == "Generated drive"
    assert payload["pointCount"] == 3
    assert len(payload["previewPoints"]) == 3
    assert list_response.status == 200
    assert list_payload["imports"][0]["filename"] == payload["filename"]
    assert list_payload["imports"][0]["originLabel"] == "Simulated"
    assert list_payload["imports"][0]["sourceType"] == "directions"
    assert list_payload["imports"][0]["distanceMeters"] == 4200


def test_google_maps_link_generates_persistent_route_preview(
    tmp_path: Path,
) -> None:
    provider = FakeDirectionsProvider()
    expanded = []

    def expand(value: str) -> str:
        expanded.append(value)
        return (
            "https://www.google.com/maps/dir/?api=1"
            "&origin=37.4158495,-122.0349283"
            "&destination=37.3920662,-122.0947471"
        )

    generated = generate_google_maps_directions_gpx(
        {
            "url": "https://maps.app.goo.gl/phase3",
            "name": "Shared Google drive",
        },
        imports_directory=tmp_path / "routes/imports",
        directions_provider=provider,
        link_expander=expand,
    )

    assert expanded == ["https://maps.app.goo.gl/phase3"]
    assert generated["name"] == "Shared Google drive"
    assert generated["sourceType"] == "google-maps"
    assert generated["sourceUrlHost"] == "www.google.com"
    assert generated["sourceWasShortened"] is True
    assert generated["requestedDestination"] == {
        "latitude": 37.3920662,
        "longitude": -122.0947471,
    }
    reloaded = imported_gpx_payloads(tmp_path / "routes/imports")[0]
    assert reloaded["sourceType"] == "google-maps"
    assert reloaded["provider"] == "Test Directions"


def test_google_maps_link_endpoint_returns_actionable_geocoding_error(
    tmp_path: Path,
) -> None:
    class GoogleMapsHandler(RouteRequestHandler):
        imports_directory = tmp_path / "routes/imports"
        generated_directory = tmp_path / "routes/generated"
        registry = RouteRegistry(tmp_path)
        timing_provider = FakeRoadTimingProvider()
        directions_provider = FakeDirectionsProvider()

    GoogleMapsHandler.manager = PlaybackManager(registry=GoogleMapsHandler.registry)
    server = ThreadingHTTPServer(("127.0.0.1", 0), GoogleMapsHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        server.server_address[1],
        timeout=5,
    )
    try:
        connection.request(
            "POST",
            "/api/routes/from-google-maps-link",
            body=json.dumps(
                {
                    "url": (
                        "https://www.google.com/maps/dir/?api=1"
                        "&origin=Stanford&destination=Berkeley"
                    )
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert response.status == 422
    assert payload["errorCode"] == "google_maps_geocoding_required"
    assert "place names" in payload["error"]


def test_import_gpx_endpoint_accepts_json(tmp_path: Path) -> None:
    class ImportHandler(RouteRequestHandler):
        imports_directory = tmp_path / "routes/imports"
        generated_directory = tmp_path / "routes/generated"
        registry = RouteRegistry(tmp_path)
        timing_provider = FakeRoadTimingProvider()

    ImportHandler.manager = PlaybackManager(registry=ImportHandler.registry)
    server = ThreadingHTTPServer(("127.0.0.1", 0), ImportHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        server.server_address[1],
        timeout=5,
    )
    try:
        body = json.dumps(
            {
                "filename": "uploaded.gpx",
                "content": UNTIMED_TRACK,
            }
        )
        connection.request(
            "POST",
            "/api/routes/import-gpx",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        response_payload = json.loads(response.read())

        connection.request("GET", "/api/routes/imports")
        list_response = connection.getresponse()
        list_payload = json.loads(list_response.read())

        connection.request("GET", "/api/routes/imports/uploaded.gpx")
        detail_response = connection.getresponse()
        detail_payload = json.loads(detail_response.read())
        source_exists_before_delete = (
            ImportHandler.imports_directory / "uploaded.gpx"
        ).is_file()

        prepare_body = json.dumps(
            {"durationSeconds": None, "timingMode": "auto"}
        )
        connection.request(
            "POST",
            "/api/routes/imports/uploaded.gpx/prepare",
            body=prepare_body,
            headers={"Content-Type": "application/json"},
        )
        prepare_response = connection.getresponse()
        prepare_payload = json.loads(prepare_response.read())

        connection.request(
            "GET",
            f"/api/routes/{prepare_payload['id']}/preview",
        )
        preview_response = connection.getresponse()
        preview_payload = json.loads(preview_response.read())

        connection.request(
            "DELETE",
            "/api/routes/imports/uploaded.gpx",
        )
        delete_response = connection.getresponse()
        delete_payload = json.loads(delete_response.read())

        connection.request("GET", "/api/routes/imports")
        empty_list_response = connection.getresponse()
        empty_list_payload = json.loads(empty_list_response.read())
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert response.status == 201
    assert response_payload["filename"] == "uploaded.gpx"
    assert response_payload["hasTimestamps"] is False
    assert source_exists_before_delete is True
    assert list_response.status == 200
    assert [item["filename"] for item in list_payload["imports"]] == ["uploaded.gpx"]
    assert detail_response.status == 200
    assert detail_payload["content"] == UNTIMED_TRACK
    assert prepare_response.status == 200
    assert prepare_payload["id"] == "generated-uploaded"
    assert prepare_payload["durationSeconds"] == 30
    assert prepare_payload["timingMode"] == "route-aware"
    assert prepare_payload["timingProvider"] == "Test Roads"
    assert len(prepare_payload["previewPoints"]) == 2
    assert preview_response.status == 200
    assert preview_payload["routeId"] == "generated-uploaded"
    assert len(preview_payload["points"]) == 61
    assert delete_response.status == 200
    assert delete_payload["deleted"] is True
    assert delete_payload["filename"] == "uploaded.gpx"
    assert delete_payload["removedRouteIds"] == ["generated-uploaded"]
    assert not (ImportHandler.imports_directory / "uploaded.gpx").exists()
    assert not (ImportHandler.generated_directory / "uploaded.track.gpx").exists()
    assert empty_list_response.status == 200
    assert empty_list_payload["imports"] == []
    assert ImportHandler.registry.all() == []


def test_prepare_imported_gpx_generates_and_registers_playback_track(
    tmp_path: Path,
) -> None:
    imports_directory = tmp_path / "routes/imports"
    generated_directory = tmp_path / "routes/generated"
    imports_directory.mkdir(parents=True)
    source_path = imports_directory / "uploaded.gpx"
    source_path.write_text(UNTIMED_TRACK, encoding="utf-8")
    registry = RouteRegistry(tmp_path)

    prepared = prepare_imported_gpx(
        source_path.name,
        {"durationSeconds": 60, "label": "Imported commute"},
        imports_directory=imports_directory,
        generated_directory=generated_directory,
        registry=registry,
    )

    assert prepared["id"] == "generated-uploaded"
    assert prepared["label"] == "Imported commute"
    assert prepared["direction"] == "custom"
    assert prepared["durationSeconds"] == 60
    assert prepared["pointCount"] == 121
    assert prepared["sourcePath"] == str(source_path)
    assert prepared["timingMode"] == "uniform"
    assert prepared["timingWarning"]
    assert registry.get("generated-uploaded").track_path.is_file()
    assert len(registry.all()) == 1

    updated = prepare_imported_gpx(
        source_path.name,
        {"durationSeconds": 120},
        imports_directory=imports_directory,
        generated_directory=generated_directory,
        registry=registry,
    )

    assert updated["durationSeconds"] == 120
    assert len(registry.all()) == 1


def test_prepare_imported_gpx_uses_provider_eta_when_duration_is_null(
    tmp_path: Path,
) -> None:
    imports_directory = tmp_path / "routes/imports"
    generated_directory = tmp_path / "routes/generated"
    imports_directory.mkdir(parents=True)
    source_path = imports_directory / "uploaded.gpx"
    source_path.write_text(UNTIMED_TRACK, encoding="utf-8")
    registry = RouteRegistry(tmp_path)

    prepared = prepare_imported_gpx(
        source_path.name,
        {"durationSeconds": None, "timingMode": "auto"},
        imports_directory=imports_directory,
        generated_directory=generated_directory,
        registry=registry,
        timing_provider=FakeRoadTimingProvider(),
    )

    assert prepared["timingMode"] == "route-aware"
    assert prepared["timingProvider"] == "Test Roads"
    assert prepared["estimatedDurationSeconds"] == 30
    assert prepared["durationSeconds"] == 30
    assert prepared["timingWarning"] == ""
    assert len(prepared["previewPoints"]) == 2
    saved = registry.get("generated-uploaded")
    assert saved.timing_mode == "route-aware"
    assert saved.timing_provider == "Test Roads"
    preview = route_preview_payload(saved.id, registry)
    assert preview["routeId"] == saved.id
    assert preview["points"][0]["latitude"] == 37.3835546


def test_delete_imported_gpx_rejects_active_route(tmp_path: Path) -> None:
    imports_directory = tmp_path / "routes/imports"
    generated_directory = tmp_path / "routes/generated"
    imports_directory.mkdir(parents=True)
    source_path = imports_directory / "uploaded.gpx"
    source_path.write_text(UNTIMED_TRACK, encoding="utf-8")
    registry = RouteRegistry(tmp_path)
    prepared = prepare_imported_gpx(
        source_path.name,
        {"durationSeconds": 60},
        imports_directory=imports_directory,
        generated_directory=generated_directory,
        registry=registry,
    )

    with pytest.raises(ApiError) as raised:
        delete_imported_gpx(
            source_path.name,
            imports_directory=imports_directory,
            generated_directory=generated_directory,
            registry=registry,
            active_route_id=prepared["id"],
        )

    assert raised.value.status == 409
    assert raised.value.code == "route_in_use"
    assert source_path.is_file()
    assert registry.get(prepared["id"]).track_path.is_file()
