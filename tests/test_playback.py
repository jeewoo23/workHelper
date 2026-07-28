from pathlib import Path

import pytest

from route_controller.gpx import generate_directional_tracks
from route_controller.playback import (
    PlaybackError,
    clear_arguments,
    play_arguments,
    play_route,
    set_arguments,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "routes" / "source" / "route_final.gpx"


def test_playback_uses_argument_array_and_absolute_route(tmp_path: Path) -> None:
    outbound, _ = generate_directional_tracks(SOURCE, tmp_path)
    arguments = play_arguments("/opt/tools/pymobiledevice3", outbound)

    assert arguments[:5] == [
        "/opt/tools/pymobiledevice3",
        "developer",
        "dvt",
        "simulate-location",
        "play",
    ]
    assert arguments[-1] == str(outbound.resolve())


def test_playback_can_request_userspace_tunnel(tmp_path: Path) -> None:
    outbound, _ = generate_directional_tracks(SOURCE, tmp_path)
    arguments = play_arguments("/opt/tools/pymobiledevice3", outbound, userspace=True)

    assert arguments == [
        "/opt/tools/pymobiledevice3",
        "developer",
        "dvt",
        "simulate-location",
        "play",
        "--userspace",
        str(outbound.resolve()),
    ]


def test_clear_arguments_are_narrow() -> None:
    assert clear_arguments("pymobiledevice3") == [
        "pymobiledevice3",
        "developer",
        "dvt",
        "simulate-location",
        "clear",
    ]


def test_clear_arguments_can_request_userspace_tunnel() -> None:
    assert clear_arguments("pymobiledevice3", userspace=True) == [
        "pymobiledevice3",
        "developer",
        "dvt",
        "simulate-location",
        "clear",
        "--userspace",
    ]


def test_set_arguments_validate_and_separate_coordinates() -> None:
    assert set_arguments("pymobiledevice3", 37.3, -122.1) == [
        "pymobiledevice3",
        "developer",
        "dvt",
        "simulate-location",
        "set",
        "--",
        "37.3",
        "-122.1",
    ]

    with pytest.raises(PlaybackError, match="Latitude"):
        set_arguments("pymobiledevice3", 91, 0)


def test_set_arguments_can_request_userspace_tunnel_before_coordinates() -> None:
    assert set_arguments("pymobiledevice3", 37.3, -122.1, userspace=True) == [
        "pymobiledevice3",
        "developer",
        "dvt",
        "simulate-location",
        "set",
        "--userspace",
        "--",
        "37.3",
        "-122.1",
    ]


def test_play_defaults_to_dry_run_without_installed_tool(tmp_path: Path) -> None:
    outbound, _ = generate_directional_tracks(SOURCE, tmp_path)
    result = play_route(outbound, executable="pymobiledevice3", execute=False)

    assert result.executed is False
    assert result.returncode is None
    assert result.arguments[-1] == str(outbound.resolve())
