from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Optional


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

    def as_dict(self) -> dict:
        return asdict(self)


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


def inspect_environment(probe_device: bool = False) -> EnvironmentReport:
    xcodebuild = shutil.which("xcodebuild")
    xcode_version = None
    if xcodebuild:
        _, xcode_version = _command_output([xcodebuild, "-version"])

    pymobiledevice3 = shutil.which("pymobiledevice3")
    device_probe_ok = None
    device_count = None
    device_probe_output = None
    if probe_device:
        if pymobiledevice3:
            command_ok, device_probe_output = _command_output(
                [pymobiledevice3, "usbmux", "list"], timeout=20
            )
            device_probe_ok = command_ok
            if command_ok:
                try:
                    devices = json.loads(device_probe_output or "[]")
                except json.JSONDecodeError:
                    pass
                else:
                    if isinstance(devices, list):
                        device_count = len(devices)
                        device_probe_ok = device_count > 0
                        if device_count == 0:
                            device_probe_output = "No USB-connected iPhone found"
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
    )
