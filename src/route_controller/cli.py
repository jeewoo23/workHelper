from __future__ import annotations

import argparse
import json
import shlex
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Optional, Sequence

from .environment import inspect_environment
from .gpx import (
    GpxValidationError,
    generate_directional_tracks,
    parse_track,
    summarize,
)
from .playback import PlaybackError, clear_route_location, play_route, set_route_location
from .server import run_server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="route-controller",
        description="Prepare and safely play timed GPX tracks on a tethered iPhone or iPad.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify = subparsers.add_parser("verify", help="Inspect local prerequisites")
    verify.add_argument(
        "--probe-device",
        action="store_true",
        help="Run read-only pymobiledevice3 USB device discovery",
    )
    verify.add_argument("--json", action="store_true", help="Print JSON")

    convert = subparsers.add_parser(
        "convert", help="Convert the L1/L2 Xcode waypoint route into timed tracks"
    )
    convert.add_argument("source", type=Path)
    convert.add_argument("output_directory", type=Path)
    convert.add_argument("--split-name", default="L2")
    convert.add_argument(
        "--interpolate-seconds",
        type=float,
        help="Insert linear track points at this second interval for smoother playback",
    )

    inspect = subparsers.add_parser("inspect", help="Validate and summarize a track")
    inspect.add_argument("route", type=Path)
    inspect.add_argument("--json", action="store_true")

    set_location = subparsers.add_parser(
        "set", help="Set one simulated coordinate for an explicit device test"
    )
    set_location.add_argument("latitude", type=float)
    set_location.add_argument("longitude", type=float)
    set_location.add_argument("--execute", action="store_true")
    set_location.add_argument("--executable")
    set_location.add_argument("--udid", help="Target one connected device by UDID")
    set_location.add_argument(
        "--userspace",
        action="store_true",
        help="Use pymobiledevice3's no-root iOS 17+ userspace tunnel",
    )

    play = subparsers.add_parser("play", help="Play a validated track")
    play.add_argument("route", type=Path)
    play.add_argument("--execute", action="store_true")
    play.add_argument("--executable")
    play.add_argument("--udid", help="Target one connected device by UDID")
    play.add_argument(
        "--userspace",
        action="store_true",
        help="Use pymobiledevice3's no-root iOS 17+ userspace tunnel",
    )
    play.add_argument(
        "--no-clear-on-interrupt",
        action="store_true",
        help="Do not restore real location after Ctrl-C",
    )

    clear = subparsers.add_parser("clear", help="Restore the device's real location")
    clear.add_argument("--execute", action="store_true")
    clear.add_argument("--executable")
    clear.add_argument("--udid", help="Target one connected device by UDID")
    clear.add_argument(
        "--userspace",
        action="store_true",
        help="Use pymobiledevice3's no-root iOS 17+ userspace tunnel",
    )

    serve = subparsers.add_parser("serve", help="Run the loopback backend and frontend")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    return parser


def _print_command(arguments: Sequence[str]) -> None:
    print(" ".join(shlex.quote(argument) for argument in arguments))


def _print_executed_result(action: str, returncode: Optional[int]) -> int:
    code = returncode or 0
    if code == 0:
        print(f"{action}: command completed successfully")
    else:
        print(f"{action}: command failed with exit code {code}", file=sys.stderr)
    return code


def _run(arguments: argparse.Namespace) -> int:
    if arguments.command == "verify":
        report = inspect_environment(probe_device=arguments.probe_device)
        if arguments.json:
            print(json.dumps(report.as_dict(), indent=2))
        else:
            print(f"macOS: {report.macos_version or 'unknown'}")
            print(f"Python: {report.python_version}")
            print(f"Xcode: {report.xcode_version or 'not found'}")
            print(
                "pymobiledevice3: "
                + (report.pymobiledevice3_path or "not installed/on PATH")
            )
            if report.device_probe_attempted:
                print(
                    "Device probe: "
                    + ("succeeded" if report.device_probe_ok else "failed")
                )
                if report.device_probe_output:
                    print(report.device_probe_output)
        return 0 if not arguments.probe_device or report.device_probe_ok else 1

    if arguments.command == "convert":
        generated = generate_directional_tracks(
            arguments.source,
            arguments.output_directory,
            split_name=arguments.split_name,
            interpolate_seconds=arguments.interpolate_seconds,
        )
        for route in generated:
            print(route)
        return 0

    if arguments.command == "inspect":
        name, points = parse_track(arguments.route)
        summary = summarize(name, points)
        payload = asdict(summary)
        if arguments.json:
            print(json.dumps(payload, default=str, indent=2))
        else:
            print(f"Route: {summary.name}")
            print(f"Points: {summary.point_count}")
            print(f"Duration: {summary.duration_seconds:.0f} seconds")
            print(
                f"Start: {summary.start.latitude}, {summary.start.longitude} "
                f"at {summary.started_at.isoformat()}"
            )
            print(
                f"End: {summary.end.latitude}, {summary.end.longitude} "
                f"at {summary.ended_at.isoformat()}"
            )
        return 0

    if arguments.command == "play":
        result = play_route(
            arguments.route,
            executable=arguments.executable,
            execute=arguments.execute,
            userspace=arguments.userspace,
            udid=arguments.udid,
            clear_on_interrupt=not arguments.no_clear_on_interrupt,
        )
        if not result.executed:
            print("Dry run; add --execute to control the connected device:")
            _print_command(result.arguments)
            return 0
        return _print_executed_result("Route playback", result.returncode)

    if arguments.command == "set":
        result = set_route_location(
            arguments.latitude,
            arguments.longitude,
            executable=arguments.executable,
            execute=arguments.execute,
            userspace=arguments.userspace,
            udid=arguments.udid,
        )
        if not result.executed:
            print("Dry run; add --execute to set the connected device's location:")
            _print_command(result.arguments)
            return 0
        return _print_executed_result("Static location set", result.returncode)

    if arguments.command == "clear":
        result = clear_route_location(
            executable=arguments.executable,
            execute=arguments.execute,
            userspace=arguments.userspace,
            udid=arguments.udid,
        )
        if not result.executed:
            print("Dry run; add --execute to restore the connected device's real location:")
            _print_command(result.arguments)
            return 0
        return _print_executed_result("Location clear", result.returncode)

    if arguments.command == "serve":
        run_server(host=arguments.host, port=arguments.port)
        return 0

    raise AssertionError(f"Unhandled command: {arguments.command}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        return _run(_parser().parse_args(argv))
    except (GpxValidationError, PlaybackError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
