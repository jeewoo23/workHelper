import json
import time
from datetime import datetime, timezone

import pytest

from route_controller.schedule import (
    LocationScheduleController,
    ScheduleValidationError,
    evaluate_schedule,
    parse_schedule,
    schedule_payload,
)


SCHEDULE = {
    "name": "Weekday routine",
    "timezone": "America/Los_Angeles",
    "entries": [
        {
            "label": "Office",
            "days": ["mon", "tue", "wed", "thu", "fri"],
            "start": "09:00",
            "end": "17:00",
            "latitude": 37.38368040757789,
            "longitude": -122.13672073499355,
        },
        {
            "label": "Gym",
            "days": ["mon", "wed", "fri"],
            "start": "17:00",
            "end": "18:30",
            "latitude": 37.4,
            "longitude": -122.1,
        },
    ],
}


class FakeProcess:
    next_pid = 7000

    def __init__(self, arguments, **kwargs):
        self.arguments = arguments
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def test_schedule_round_trip_and_weekday_evaluation() -> None:
    schedule = parse_schedule(SCHEDULE)

    assert schedule_payload(schedule) == SCHEDULE
    monday_morning = evaluate_schedule(
        schedule,
        datetime(2026, 8, 17, 16, 30, tzinfo=timezone.utc),
    )
    assert monday_morning.active is not None
    assert monday_morning.active.entry.label == "Office"
    assert monday_morning.next_transition_at.isoformat() == "2026-08-17T17:00:00-07:00"

    monday_evening = evaluate_schedule(
        schedule,
        datetime(2026, 8, 18, 2, 0, tzinfo=timezone.utc),
    )
    assert monday_evening.active is None
    assert monday_evening.next_occurrence is not None
    assert monday_evening.next_occurrence.entry.label == "Office"
    assert monday_evening.next_occurrence.starts_at.isoformat() == "2026-08-18T09:00:00-07:00"


def test_overnight_window_uses_the_selected_start_day() -> None:
    schedule = parse_schedule(
        {
            "name": "Friday overnight",
            "timezone": "America/Los_Angeles",
            "entries": [
                {
                    "label": "Night location",
                    "days": ["fri"],
                    "start": "22:00",
                    "end": "02:00",
                    "latitude": 37.3,
                    "longitude": -122.2,
                }
            ],
        }
    )

    saturday_at_one = evaluate_schedule(
        schedule,
        datetime(2026, 8, 22, 8, 0, tzinfo=timezone.utc),
    )

    assert saturday_at_one.active is not None
    assert saturday_at_one.active.entry.label == "Night location"
    assert saturday_at_one.active.ends_at.isoformat() == "2026-08-22T02:00:00-07:00"


def test_schedule_rejects_overlaps_and_invalid_fields() -> None:
    overlapping = json.loads(json.dumps(SCHEDULE))
    overlapping["entries"][1]["start"] = "16:59"
    with pytest.raises(ScheduleValidationError, match="overlaps"):
        parse_schedule(overlapping)

    invalid_timezone = json.loads(json.dumps(SCHEDULE))
    invalid_timezone["timezone"] = "Not/A_Timezone"
    with pytest.raises(ScheduleValidationError, match="Unknown timezone"):
        parse_schedule(invalid_timezone)

    no_days = json.loads(json.dumps(SCHEDULE))
    no_days["entries"][0]["days"] = []
    with pytest.raises(ScheduleValidationError, match="repeat day"):
        parse_schedule(no_days)


def test_controller_applies_clears_persists_and_resumes(tmp_path) -> None:
    current_time = [datetime(2026, 8, 17, 16, 30, tzinfo=timezone.utc)]
    set_calls = []
    clear_calls = []
    power_processes = []

    def process_factory(arguments, **kwargs):
        process = FakeProcess(arguments, **kwargs)
        power_processes.append(process)
        return process

    controller = LocationScheduleController(
        tmp_path / "schedule.json",
        set_location=lambda latitude, longitude: set_calls.append((latitude, longitude)),
        clear_location=lambda: clear_calls.append(True),
        now=lambda: current_time[0],
        poll_seconds=0.01,
        process_factory=process_factory,
    )
    try:
        active = controller.save_and_start(SCHEDULE)

        assert active["state"] == "active"
        assert active["activeWindow"]["label"] == "Office"
        assert active["preventingIdleSleep"] is True
        assert set_calls == [(37.38368040757789, -122.13672073499355)]
        saved = json.loads((tmp_path / "schedule.json").read_text())
        assert saved["enabled"] is True

        current_time[0] = datetime(2026, 8, 18, 2, 0, tzinfo=timezone.utc)
        deadline = time.monotonic() + 1
        while not clear_calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert clear_calls == [True]
        assert controller.status()["state"] == "waiting"
    finally:
        controller.shutdown()

    resumed_calls = []
    current_time[0] = datetime(2026, 8, 18, 16, 30, tzinfo=timezone.utc)
    resumed = LocationScheduleController(
        tmp_path / "schedule.json",
        set_location=lambda latitude, longitude: resumed_calls.append((latitude, longitude)),
        clear_location=lambda: None,
        now=lambda: current_time[0],
        poll_seconds=0.01,
        process_factory=process_factory,
    )
    try:
        deadline = time.monotonic() + 1
        while not resumed_calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert resumed_calls == [(37.38368040757789, -122.13672073499355)]
        stopped = resumed.stop(clear_location=True)
        assert stopped["state"] == "disabled"
        assert stopped["enabled"] is False
        assert power_processes[-1].terminated is True
        assert json.loads((tmp_path / "schedule.json").read_text())["enabled"] is False
    finally:
        resumed.shutdown()
