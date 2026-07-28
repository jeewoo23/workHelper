import json

from route_controller import environment


def test_empty_device_list_is_not_reported_as_connected(monkeypatch) -> None:
    monkeypatch.setattr(environment.shutil, "which", lambda command: f"/bin/{command}")

    def fake_output(arguments, timeout=15):
        if arguments[-2:] == ["usbmux", "list"]:
            return True, json.dumps([])
        return True, "Xcode test version"

    monkeypatch.setattr(environment, "_command_output", fake_output)

    report = environment.inspect_environment(probe_device=True)

    assert report.device_probe_ok is False
    assert report.device_count == 0
    assert report.device_probe_output == "No USB-connected iPhone found"


def test_nonempty_device_list_is_reported_as_connected(monkeypatch) -> None:
    monkeypatch.setattr(environment.shutil, "which", lambda command: f"/bin/{command}")

    def fake_output(arguments, timeout=15):
        if arguments[-2:] == ["usbmux", "list"]:
            return True, json.dumps([{"Identifier": "test-device"}])
        return True, "Xcode test version"

    monkeypatch.setattr(environment, "_command_output", fake_output)

    report = environment.inspect_environment(probe_device=True)

    assert report.device_probe_ok is True
    assert report.device_count == 1
