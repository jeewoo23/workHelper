import json

from route_controller import environment


def test_empty_device_list_is_not_reported_as_connected(monkeypatch) -> None:
    monkeypatch.setattr(environment.shutil, "which", lambda command: f"/bin/{command}")

    def fake_output(arguments, timeout=15):
        if arguments[-3:] == ["usbmux", "list", "--usb"]:
            return True, json.dumps([])
        return True, "Xcode test version"

    monkeypatch.setattr(environment, "_command_output", fake_output)

    report = environment.inspect_environment(probe_device=True)

    assert report.device_probe_ok is False
    assert report.device_count == 0
    assert report.device_probe_output == "No USB-connected iPhone or iPad found"
    assert report.devices == ()


def test_nonempty_device_list_is_reported_as_connected(monkeypatch) -> None:
    monkeypatch.setattr(environment.shutil, "which", lambda command: f"/bin/{command}")

    def fake_output(arguments, timeout=15):
        if arguments[-3:] == ["usbmux", "list", "--usb"]:
            return True, json.dumps(
                [
                    {
                        "Identifier": "00008110-TEST-IPAD",
                        "DeviceName": "Test iPad",
                        "DeviceClass": "iPad",
                        "ProductType": "iPad14,5",
                        "ProductVersion": "18.6",
                        "ConnectionType": "USB",
                    }
                ]
            )
        return True, "Xcode test version"

    monkeypatch.setattr(environment, "_command_output", fake_output)

    report = environment.inspect_environment(probe_device=True)

    assert report.device_probe_ok is True
    assert report.device_count == 1
    assert report.devices[0].device_class == "iPad"
    assert report.devices[0].os_name == "iPadOS"
    assert report.devices[0].compatible is True
    assert report.as_dict()["devices"][0]["name"] == "Test iPad"


def test_old_ipados_is_discovered_but_not_compatible(monkeypatch) -> None:
    monkeypatch.setattr(environment.shutil, "which", lambda command: f"/bin/{command}")

    def fake_output(arguments, timeout=15):
        if arguments[-3:] == ["usbmux", "list", "--usb"]:
            return True, json.dumps(
                [
                    {
                        "Identifier": "old-ipad",
                        "DeviceClass": "iPad",
                        "ProductVersion": "17.3.1",
                        "ConnectionType": "USB",
                    }
                ]
            )
        return True, "Xcode test version"

    monkeypatch.setattr(environment, "_command_output", fake_output)

    report = environment.inspect_environment(probe_device=True)

    assert report.device_probe_ok is False
    assert report.device_count == 1
    assert report.devices[0].compatible is False
    assert "17.4 or later" in report.device_probe_output


def test_malformed_device_inventory_fails_safely(monkeypatch) -> None:
    monkeypatch.setattr(environment.shutil, "which", lambda command: f"/bin/{command}")

    def fake_output(arguments, timeout=15):
        if arguments[-3:] == ["usbmux", "list", "--usb"]:
            return True, "not-json"
        return True, "Xcode test version"

    monkeypatch.setattr(environment, "_command_output", fake_output)

    report = environment.inspect_environment(probe_device=True)

    assert report.device_probe_ok is False
    assert report.device_count is None
    assert report.device_probe_output == "USB device discovery returned invalid JSON"
