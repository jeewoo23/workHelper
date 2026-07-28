from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .gpx import parse_track


class PlaybackError(RuntimeError):
    """Raised when route playback cannot be started or stopped safely."""


@dataclass(frozen=True)
class PlaybackResult:
    arguments: tuple[str, ...]
    returncode: Optional[int]
    executed: bool


def resolve_executable(explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    located = shutil.which("pymobiledevice3")
    if not located:
        raise PlaybackError(
            "pymobiledevice3 is not installed or is not on PATH. "
            "Install the device extra or follow docs/BUILD_SPEC.md."
        )
    return located


def _with_device_options(arguments: list[str], *, userspace: bool) -> list[str]:
    if userspace:
        arguments.append("--userspace")
    return arguments


def play_arguments(executable: str, route: Path, *, userspace: bool = False) -> list[str]:
    return _with_device_options(
        [
            executable,
            "developer",
            "dvt",
            "simulate-location",
            "play",
        ],
        userspace=userspace,
    ) + [
        str(route.resolve()),
    ]


def clear_arguments(executable: str, *, userspace: bool = False) -> list[str]:
    return _with_device_options(
        [
            executable,
            "developer",
            "dvt",
            "simulate-location",
            "clear",
        ],
        userspace=userspace,
    )


def set_arguments(
    executable: str, latitude: float, longitude: float, *, userspace: bool = False
) -> list[str]:
    if not -90 <= latitude <= 90:
        raise PlaybackError(f"Latitude is outside [-90, 90]: {latitude}")
    if not -180 <= longitude <= 180:
        raise PlaybackError(f"Longitude is outside [-180, 180]: {longitude}")
    return _with_device_options(
        [
            executable,
            "developer",
            "dvt",
            "simulate-location",
            "set",
        ],
        userspace=userspace,
    ) + [
        "--",
        str(latitude),
        str(longitude),
    ]


def clear_location(executable: str) -> int:
    return subprocess.run(clear_arguments(executable), check=False).returncode


def play_route(
    route: Path,
    *,
    executable: Optional[str] = None,
    execute: bool = False,
    userspace: bool = False,
    clear_on_interrupt: bool = True,
) -> PlaybackResult:
    route_name, points = parse_track(route)
    if len(points) < 2:
        raise PlaybackError(f"Route {route_name!r} must contain at least two points")

    command_executable = executable or shutil.which("pymobiledevice3") or "pymobiledevice3"
    arguments = play_arguments(command_executable, route, userspace=userspace)
    if not execute:
        return PlaybackResult(tuple(arguments), None, False)

    command_executable = resolve_executable(executable)
    arguments = play_arguments(command_executable, route, userspace=userspace)
    process = subprocess.Popen(arguments)
    try:
        return PlaybackResult(tuple(arguments), process.wait(), True)
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if clear_on_interrupt:
            clear_location(command_executable)
        return PlaybackResult(tuple(arguments), 130, True)


def clear_route_location(
    *, executable: Optional[str] = None, execute: bool = False, userspace: bool = False
) -> PlaybackResult:
    command_executable = executable or shutil.which("pymobiledevice3") or "pymobiledevice3"
    arguments = clear_arguments(command_executable, userspace=userspace)
    if not execute:
        return PlaybackResult(tuple(arguments), None, False)
    command_executable = resolve_executable(executable)
    arguments = clear_arguments(command_executable, userspace=userspace)
    return PlaybackResult(
        tuple(arguments),
        subprocess.run(arguments, check=False).returncode,
        True,
    )


def set_route_location(
    latitude: float,
    longitude: float,
    *,
    executable: Optional[str] = None,
    execute: bool = False,
    userspace: bool = False,
) -> PlaybackResult:
    command_executable = executable or shutil.which("pymobiledevice3") or "pymobiledevice3"
    arguments = set_arguments(
        command_executable, latitude, longitude, userspace=userspace
    )
    if not execute:
        return PlaybackResult(tuple(arguments), None, False)
    command_executable = resolve_executable(executable)
    arguments = set_arguments(command_executable, latitude, longitude, userspace=userspace)
    return PlaybackResult(
        tuple(arguments),
        subprocess.run(arguments, check=False).returncode,
        True,
    )
