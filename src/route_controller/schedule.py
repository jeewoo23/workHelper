from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, time as wall_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MAX_SCHEDULE_ENTRIES = 64
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_UNKNOWN = object()


class ScheduleError(ValueError):
    """Base error for deterministic location schedules."""


class ScheduleValidationError(ScheduleError):
    """Raised when a schedule cannot be applied safely."""


class ScheduleStorageError(ScheduleError):
    """Raised when a schedule cannot be persisted."""


@dataclass(frozen=True)
class ScheduleEntry:
    id: str
    label: str
    days: tuple[int, ...]
    start: wall_time
    end: wall_time
    latitude: float
    longitude: float


@dataclass(frozen=True)
class LocationSchedule:
    name: str
    timezone_name: str
    entries: tuple[ScheduleEntry, ...]


@dataclass(frozen=True)
class ScheduleOccurrence:
    entry: ScheduleEntry
    starts_at: datetime
    ends_at: datetime

    @property
    def key(self) -> tuple[str, str]:
        return self.entry.id, self.starts_at.isoformat()


@dataclass(frozen=True)
class ScheduleMoment:
    active: Optional[ScheduleOccurrence]
    next_occurrence: Optional[ScheduleOccurrence]
    next_transition_at: Optional[datetime]


def parse_schedule(payload: Any) -> LocationSchedule:
    if not isinstance(payload, dict) or set(payload) != {"name", "timezone", "entries"}:
        raise ScheduleValidationError(
            "A schedule needs exactly name, timezone, and entries"
        )
    name = _required_string(payload["name"], "Schedule name", 120)
    timezone_name = _timezone(payload["timezone"]).key
    raw_entries = payload["entries"]
    if not isinstance(raw_entries, list) or not 1 <= len(raw_entries) <= MAX_SCHEDULE_ENTRIES:
        raise ScheduleValidationError(
            f"A schedule must contain 1 to {MAX_SCHEDULE_ENTRIES} location windows"
        )

    entries: list[ScheduleEntry] = []
    expected_fields = {"label", "days", "start", "end", "latitude", "longitude"}
    for index, raw in enumerate(raw_entries, 1):
        if not isinstance(raw, dict) or set(raw) != expected_fields:
            raise ScheduleValidationError(f"Location window {index} has invalid fields")
        raw_days = raw["days"]
        if not isinstance(raw_days, list) or not raw_days:
            raise ScheduleValidationError(
                f"Location window {index} needs at least one repeat day"
            )
        if any(day not in DAY_NAMES for day in raw_days) or len(set(raw_days)) != len(raw_days):
            raise ScheduleValidationError(
                f"Location window {index} has invalid or duplicate repeat days"
            )
        start = _parse_time(raw["start"], f"Location window {index} start")
        end = _parse_time(raw["end"], f"Location window {index} end")
        if start == end:
            raise ScheduleValidationError(
                f"Location window {index} start and end times must differ"
            )
        entries.append(
            ScheduleEntry(
                id=f"window-{index}",
                label=_required_string(
                    raw["label"], f"Location window {index} label", 100
                ),
                days=tuple(sorted(DAY_NAMES.index(day) for day in raw_days)),
                start=start,
                end=end,
                latitude=_coordinate(
                    raw["latitude"], -90, 90, f"Location window {index} latitude"
                ),
                longitude=_coordinate(
                    raw["longitude"],
                    -180,
                    180,
                    f"Location window {index} longitude",
                ),
            )
        )

    schedule = LocationSchedule(
        name=name,
        timezone_name=timezone_name,
        entries=tuple(entries),
    )
    _validate_no_overlaps(schedule)
    return schedule


def schedule_payload(schedule: LocationSchedule) -> dict[str, Any]:
    return {
        "name": schedule.name,
        "timezone": schedule.timezone_name,
        "entries": [
            {
                "label": entry.label,
                "days": [DAY_NAMES[day] for day in entry.days],
                "start": entry.start.strftime("%H:%M"),
                "end": entry.end.strftime("%H:%M"),
                "latitude": entry.latitude,
                "longitude": entry.longitude,
            }
            for entry in schedule.entries
        ],
    }


def evaluate_schedule(
    schedule: LocationSchedule,
    now: Optional[datetime] = None,
) -> ScheduleMoment:
    zone = ZoneInfo(schedule.timezone_name)
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ScheduleValidationError("Schedule evaluation requires a timezone-aware time")
    local_now = instant.astimezone(zone)
    occurrences = _occurrences_around(schedule, local_now.date(), days_ahead=8)
    active = next(
        (
            occurrence
            for occurrence in occurrences
            if occurrence.starts_at <= local_now < occurrence.ends_at
        ),
        None,
    )
    if active is not None:
        return ScheduleMoment(
            active=active,
            next_occurrence=None,
            next_transition_at=active.ends_at,
        )
    upcoming = next(
        (occurrence for occurrence in occurrences if occurrence.starts_at > local_now),
        None,
    )
    return ScheduleMoment(
        active=None,
        next_occurrence=upcoming,
        next_transition_at=upcoming.starts_at if upcoming else None,
    )


class LocationScheduleController:
    """Persist and execute one recurring schedule behind a small interface."""

    def __init__(
        self,
        store_path: Path,
        *,
        set_location: Callable[[float, float], Any],
        clear_location: Callable[[], Any],
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        poll_seconds: float = 1,
        process_factory: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self._store_path = store_path
        self._set_location = set_location
        self._clear_location = clear_location
        self._now = now
        self._poll_seconds = max(0.05, float(poll_seconds))
        self._process_factory = process_factory
        self._lock = threading.Lock()
        self._operation_lock = threading.Lock()
        self._wake = threading.Event()
        self._shutdown = False
        self._generation = 0
        self._schedule: Optional[LocationSchedule] = None
        self._enabled = False
        self._applied_key: Any = _UNKNOWN
        self._last_applied_at: Optional[str] = None
        self._error = ""
        self._power_process: Any = None
        self._power_warning = ""
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="location-schedule",
        )
        self._load()
        self._thread.start()

    def save_and_start(self, payload: Any) -> dict[str, Any]:
        schedule = parse_schedule(payload)
        self._write(schedule, enabled=True)
        with self._lock:
            self._schedule = schedule
            self._enabled = True
            self._generation += 1
            self._applied_key = _UNKNOWN
            self._error = ""
            self._ensure_power_assertion_locked()
        self._wake.set()
        self._reconcile_once()
        return self.status()

    def stop(self, *, clear_location: bool = True) -> dict[str, Any]:
        with self._lock:
            schedule = self._schedule
        if schedule is not None:
            self._write(schedule, enabled=False)
        with self._lock:
            self._enabled = False
            self._generation += 1
            self._applied_key = _UNKNOWN
            self._last_applied_at = None
            self._error = ""
            self._stop_power_assertion_locked()
        self._wake.set()
        if clear_location:
            with self._operation_lock:
                try:
                    self._clear_location()
                except Exception as error:
                    with self._lock:
                        self._error = str(error)
        return self.status()

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown = True
            self._stop_power_assertion_locked()
        self._wake.set()
        self._thread.join(timeout=3)

    def status(self) -> dict[str, Any]:
        with self._lock:
            schedule = self._schedule
            enabled = self._enabled
            error = self._error
            last_applied_at = self._last_applied_at
            power_process = self._power_process
            power_warning = self._power_warning
        moment = evaluate_schedule(schedule, self._now()) if schedule and enabled else None
        active = moment.active if moment else None
        upcoming = moment.next_occurrence if moment else None
        preventing_idle_sleep = (
            power_process is not None and power_process.poll() is None
        )
        state = (
            "error"
            if error
            else "disabled"
            if not enabled
            else "active"
            if active
            else "waiting"
        )
        payload: dict[str, Any] = {
            "state": state,
            "enabled": enabled,
            "schedule": schedule_payload(schedule) if schedule else None,
            "activeWindow": _occurrence_payload(active) if active else None,
            "nextWindow": _occurrence_payload(upcoming) if upcoming else None,
            "nextTransitionAt": (
                moment.next_transition_at.isoformat() if moment and moment.next_transition_at else None
            ),
            "lastAppliedAt": last_applied_at,
            "preventingIdleSleep": preventing_idle_sleep,
        }
        if error:
            payload["error"] = error
        if power_warning:
            payload["powerWarning"] = power_warning
        return payload

    def _run(self) -> None:
        while True:
            self._wake.wait(self._poll_seconds)
            self._wake.clear()
            with self._lock:
                if self._shutdown:
                    return
                enabled = self._enabled
                if enabled:
                    self._ensure_power_assertion_locked()
            if enabled:
                self._reconcile_once()

    def _reconcile_once(self) -> None:
        with self._lock:
            if not self._enabled or self._schedule is None or self._shutdown:
                return
            schedule = self._schedule
            generation = self._generation
        moment = evaluate_schedule(schedule, self._now())
        desired_key: Any = moment.active.key if moment.active else None
        with self._lock:
            if (
                generation != self._generation
                or not self._enabled
                or (desired_key == self._applied_key and not self._error)
            ):
                return

        with self._operation_lock:
            with self._lock:
                if (
                    generation != self._generation
                    or not self._enabled
                    or (desired_key == self._applied_key and not self._error)
                ):
                    return
            try:
                if moment.active is None:
                    self._clear_location()
                else:
                    entry = moment.active.entry
                    self._set_location(entry.latitude, entry.longitude)
            except Exception as error:
                with self._lock:
                    if generation == self._generation:
                        self._error = str(error)
                return
            with self._lock:
                if generation != self._generation or not self._enabled:
                    return
                self._applied_key = desired_key
                self._last_applied_at = (
                    self._now().astimezone(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
                self._error = ""

    def _ensure_power_assertion_locked(self) -> None:
        if self._power_process is not None and self._power_process.poll() is None:
            return
        self._power_process = None
        try:
            self._power_process = self._process_factory(
                ["caffeinate", "-i", "-w", str(os.getpid())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            self._power_warning = ""
        except OSError as error:
            self._power_warning = (
                f"Mac idle-sleep prevention could not start: {error}"
            )

    def _stop_power_assertion_locked(self) -> None:
        process = self._power_process
        self._power_process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    def _load(self) -> None:
        try:
            raw = json.loads(self._store_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or set(raw) != {"version", "enabled", "schedule"}:
                raise ScheduleValidationError("Saved schedule has an invalid format")
            if raw["version"] != 1 or not isinstance(raw["enabled"], bool):
                raise ScheduleValidationError("Saved schedule has an unsupported version")
            schedule = parse_schedule(raw["schedule"])
        except FileNotFoundError:
            return
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ScheduleValidationError) as error:
            self._error = f"Saved schedule could not be loaded: {error}"
            return
        self._schedule = schedule
        self._enabled = raw["enabled"]
        if self._enabled:
            self._ensure_power_assertion_locked()

    def _write(self, schedule: LocationSchedule, *, enabled: bool) -> None:
        content = {
            "version": 1,
            "enabled": enabled,
            "schedule": schedule_payload(schedule),
        }
        temporary_path = self._store_path.with_name(
            f".{self._store_path.name}.{time.time_ns()}.tmp"
        )
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(
                json.dumps(content, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary_path, self._store_path)
        except OSError as error:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise ScheduleStorageError(f"Schedule could not be saved: {error}") from error


def _occurrences_around(
    schedule: LocationSchedule,
    local_day: date,
    *,
    days_ahead: int,
) -> list[ScheduleOccurrence]:
    zone = ZoneInfo(schedule.timezone_name)
    occurrences: list[ScheduleOccurrence] = []
    for offset in range(-1, days_ahead + 1):
        start_day = local_day + timedelta(days=offset)
        for entry in schedule.entries:
            if start_day.weekday() not in entry.days:
                continue
            starts_at = datetime.combine(start_day, entry.start, tzinfo=zone)
            end_day = start_day + timedelta(days=entry.end <= entry.start)
            ends_at = datetime.combine(end_day, entry.end, tzinfo=zone)
            occurrences.append(
                ScheduleOccurrence(
                    entry=entry,
                    starts_at=starts_at,
                    ends_at=ends_at,
                )
            )
    return sorted(occurrences, key=lambda value: value.starts_at)


def _validate_no_overlaps(schedule: LocationSchedule) -> None:
    monday = date(2024, 1, 1)
    occurrences = _occurrences_around(schedule, monday, days_ahead=7)
    for previous, current in zip(occurrences, occurrences[1:]):
        if current.starts_at < previous.ends_at:
            raise ScheduleValidationError(
                f'"{current.entry.label}" overlaps "{previous.entry.label}"'
            )


def _occurrence_payload(occurrence: ScheduleOccurrence) -> dict[str, Any]:
    entry = occurrence.entry
    return {
        "id": entry.id,
        "label": entry.label,
        "latitude": entry.latitude,
        "longitude": entry.longitude,
        "startsAt": occurrence.starts_at.isoformat(),
        "endsAt": occurrence.ends_at.isoformat(),
    }


def _required_string(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScheduleValidationError(f"{label} is required")
    value = value.strip()
    if len(value) > maximum:
        raise ScheduleValidationError(f"{label} must be {maximum} characters or fewer")
    return value


def _timezone(value: Any) -> ZoneInfo:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 80:
        raise ScheduleValidationError("Timezone must be a valid IANA timezone")
    try:
        return ZoneInfo(value.strip())
    except ZoneInfoNotFoundError as error:
        raise ScheduleValidationError(f"Unknown timezone: {value.strip()}") from error


def _parse_time(value: Any, label: str) -> wall_time:
    if not isinstance(value, str) or not TIME_PATTERN.fullmatch(value):
        raise ScheduleValidationError(f"{label} must use 24-hour HH:MM time")
    return wall_time(hour=int(value[:2]), minute=int(value[3:]))


def _coordinate(value: Any, minimum: float, maximum: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScheduleValidationError(f"{label} must be a number")
    coordinate = float(value)
    if not minimum <= coordinate <= maximum:
        raise ScheduleValidationError(
            f"{label} must be between {minimum:g} and {maximum:g}"
        )
    return coordinate
