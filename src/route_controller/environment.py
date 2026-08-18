from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Optional


SUPPORTED_DEVICE_CLASSES = {"iPhone", "iPad"}
MINIMUM_DVT_VERSION = (17, 4)


@dataclass(frozen=True)
class DeviceTarget:
    identifier: str
    name: str
    device_class: str
    product_type: str
    product_version: str
    connection_type: str
    os_name: str
    supported: bool
    compatible: bool
    compatibility_message: str = ""

    @property
    def display_identifier(self) -> str:
        if len(self.identifier) <= 14:
            return self.identifier
        return f"{self.identifier[:8]}…{self.identifier[-4:]}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.identifier,
            "displayId": self.display_identifier,
            "name": self.name,
            "deviceClass": self.device_class,
            "productType": self.product_type,
            "productVersion": self.product_version,
            "osName": self.os_name,
            "connectionType": self.connection_type,
            "supported": self.supported,
            "compatible": self.compatible,
            "compatibilityMessage": self.compatibility_message,
        }


@dataclass(frozen=True)
class EnvironmentReport:
    macos_version: str
    python_version: str
    xcode_version: Optional[str]
    pymobiledevice3_path: Optional[str]
    device_probe_attempted: bool
    device_probe_ok: Optional[bool]
    device_count: Optional[int]
    device_probe_output: Optional[str]
    devices: tuple[DeviceTarget, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "macos_version": self.macos_version,
            "python_version": self.python_version,
            "xcode_version": self.xcode_version,
            "pymobiledevice3_path": self.pymobiledevice3_path,
            "device_probe_attempted": self.device_probe_attempted,
            "device_probe_ok": self.device_probe_ok,
            "device_count": self.device_count,
            "device_probe_output": self.device_probe_output,
            "devices": [device.as_dict() for device in self.devices],
        }


def _command_output(arguments: list[str], timeout: int = 15) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    output = "\n".join(
        part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
    )
    return completed.returncode == 0, output


def _record_value(record: dict[str, Any], *names: str) -> str:
    for name in names:
        value = record.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _version_tuple(value: str) -> tuple[int, ...]:
    match = re.match(r"^(\d+)(?:\.(\d+))?", value.strip())
    if not match:
        return ()
    return tuple(int(part or 0) for part in match.groups())


def device_target_from_record(record: dict[str, Any]) -> Optional[DeviceTarget]:
    identifier = _record_value(
        record, "Identifier", "UniqueDeviceID", "UDID", "udid", "identifier"
    )
    if not identifier:
        return None

    product_type = _record_value(record, "ProductType", "productType")
    device_class = _record_value(record, "DeviceClass", "deviceClass")
    if not device_class:
        if product_type.startswith("iPad"):
            device_class = "iPad"
        elif product_type.startswith("iPhone"):
            device_class = "iPhone"

    product_version = _record_value(record, "ProductVersion", "productVersion")
    connection_type = _record_value(
        record, "ConnectionType", "connectionType"
    ) or "USB"
    supported = device_class in SUPPORTED_DEVICE_CLASSES
    version = _version_tuple(product_version)
    usb_connected = connection_type.casefold() == "usb"
    compatible = supported and usb_connected and version >= MINIMUM_DVT_VERSION
    os_name = (
        "iPadOS" if device_class == "iPad"
        else "iOS" if device_class == "iPhone"
        else "OS"
    )

    if not supported:
        compatibility_message = (
            f"{device_class or product_type or 'This device'} is not supported; "
            "connect an iPhone or iPad."
        )
    elif not usb_connected:
        compatibility_message = (
            f"{device_class} is connected over {connection_type}, not USB. "
            "Connect it with a data-capable cable."
        )
    elif not version:
        compatibility_message = (
            f"The {device_class} OS version could not be determined. Unlock and "
            "reconnect it, then retry."
        )
    elif version < MINIMUM_DVT_VERSION:
        compatibility_message = (
            f"{os_name} {product_version} is outside the initial supported scope. "
            f"Use {os_name} 17.4 or later."
        )
    else:
        compatibility_message = ""

    return DeviceTarget(
        identifier=identifier,
        name=_record_value(record, "DeviceName", "deviceName")
        or device_class
        or "Apple device",
        device_class=device_class or "Unknown",
        product_type=product_type or device_class or "Unknown",
        product_version=product_version,
        connection_type=connection_type,
        os_name=os_name,
        supported=supported,
        compatible=compatible,
        compatibility_message=compatibility_message,
    )


def _parse_devices(output: str) -> Optional[tuple[DeviceTarget, ...]]:
    try:
        records = json.loads(output or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(records, list):
        return None

    devices: list[DeviceTarget] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        target = device_target_from_record(record)
        if target is None or target.identifier in seen:
            continue
        seen.add(target.identifier)
        devices.append(target)
    return tuple(devices)


def inspect_environment(probe_device: bool = False) -> EnvironmentReport:
    xcodebuild = shutil.which("xcodebuild")
    xcode_version = None
    if xcodebuild:
        _, xcode_version = _command_output([xcodebuild, "-version"])

    pymobiledevice3 = shutil.which("pymobiledevice3")
    device_probe_ok = None
    device_count = None
    device_probe_output = None
    devices: tuple[DeviceTarget, ...] = ()
    if probe_device:
        if pymobiledevice3:
            command_ok, raw_output = _command_output(
                [pymobiledevice3, "usbmux", "list", "--usb"], timeout=20
            )
            parsed_devices = _parse_devices(raw_output) if command_ok else None
            devices = parsed_devices or ()
            device_count = len(devices) if parsed_devices is not None else None
            compatible_count = sum(device.compatible for device in devices)
            device_probe_ok = (
                command_ok and parsed_devices is not None and compatible_count > 0
            )
            if not command_ok:
                device_probe_output = raw_output or "USB device discovery failed"
            elif parsed_devices is None:
                device_probe_output = "USB device discovery returned invalid JSON"
            elif not devices:
                device_probe_output = "No USB-connected iPhone or iPad found"
            elif not compatible_count:
                device_probe_output = "; ".join(
                    device.compatibility_message
                    for device in devices
                    if device.compatibility_message
                ) or "No compatible USB device found"
            else:
                device_probe_output = raw_output
        else:
            device_probe_ok = False
            device_probe_output = (
                "pymobiledevice3 is not installed or is not on PATH"
            )

    return EnvironmentReport(
        macos_version=platform.mac_ver()[0],
        python_version=sys.version.split()[0],
        xcode_version=xcode_version,
        pymobiledevice3_path=pymobiledevice3,
        device_probe_attempted=probe_device,
        device_probe_ok=device_probe_ok,
        device_count=device_count,
        device_probe_output=device_probe_output,
        devices=devices,
    )
