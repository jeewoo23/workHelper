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


def play_arguments(executable: str, route: Path) -> list[str]:
    return [
        executable,
        "developer",
        "dvt",
        "simulate-location",
        "play",
        str(route.resolve()),
    ]


def clear_arguments(executable: str) -> list[str]:
    return [
        executable,
        "developer",
        "dvt",
        "simulate-location",
        "clear",
    ]


def set_arguments(executable: str, latitude: float, longitude: float) -> list[str]:
    if not -90 <= latitude <= 90:
        raise PlaybackError(f"Latitude is outside [-90, 90]: {latitude}")
    if not -180 <= longitude <= 180:
        raise PlaybackError(f"Longitude is outside [-180, 180]: {longitude}")
    return [
        executable,
        "developer",
        "dvt",
        "simulate-location",
        "set",
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
    clear_on_interrupt: bool = True,
) -> PlaybackResult:
    route_name, points = parse_track(route)
    if len(points) < 2:
        raise PlaybackError(f"Route {route_name!r} must contain at least two points")

    command_executable = executable or shutil.which("pymobiledevice3") or "pymobiledevice3"
    arguments = play_arguments(command_executable, route)
    if not execute:
        return PlaybackResult(tuple(arguments), None, False)

    command_executable = resolve_executable(executable)
    arguments = play_arguments(command_executable, route)
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
    *, executable: Optional[str] = None, execute: bool = False
) -> PlaybackResult:
    command_executable = executable or shutil.which("pymobiledevice3") or "pymobiledevice3"
    arguments = clear_arguments(command_executable)
    if not execute:
        return PlaybackResult(tuple(arguments), None, False)
    command_executable = resolve_executable(executable)
    arguments = clear_arguments(command_executable)
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
) -> PlaybackResult:
    command_executable = executable or shutil.which("pymobiledevice3") or "pymobiledevice3"
    arguments = set_arguments(command_executable, latitude, longitude)
    if not execute:
        return PlaybackResult(tuple(arguments), None, False)
    command_executable = resolve_executable(executable)
    arguments = set_arguments(command_executable, latitude, longitude)
    return PlaybackResult(
        tuple(arguments),
        subprocess.run(arguments, check=False).returncode,
        True,
    )
