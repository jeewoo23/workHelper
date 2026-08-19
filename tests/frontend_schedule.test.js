const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const appSource = fs.readFileSync(path.join(root, "frontend", "app.js"), "utf8");
const stylesSource = fs.readFileSync(path.join(root, "frontend", "styles.css"), "utf8");

test("weekly schedule exposes deterministic location windows", () => {
  assert.match(appSource, /data-sidebar-section="location-schedule"/);
  assert.match(appSource, /data-schedule-field="latitude"/);
  assert.match(appSource, /data-schedule-field="longitude"/);
  assert.match(appSource, /data-schedule-field="start"/);
  assert.match(appSource, /data-schedule-field="end"/);
  assert.match(appSource, /data-schedule-day=/);
  assert.match(appSource, /\/api\/schedule\/activate/);
  assert.match(appSource, /\/api\/schedule\/stop/);
});

test("schedule UI explains recurrence and overnight behavior", () => {
  assert.match(appSource, /Windows repeat every selected weekday/);
  assert.match(appSource, /continues overnight/);
  assert.match(appSource, /Real GPS is used outside windows/);
  assert.match(stylesSource, /\.schedule-window\s*\{/);
  assert.match(stylesSource, /\.schedule-days\s*\{/);
});

test("schedule coordinates can be selected directly from the map", () => {
  assert.match(appSource, /data-schedule-map-pick=/);
  assert.match(appSource, /function toggleScheduleMapPick\(index\)/);
  assert.match(appSource, /entry\.latitude = point\.lat\.toFixed\(7\)/);
  assert.match(appSource, /entry\.longitude = point\.lon\.toFixed\(7\)/);
  assert.match(stylesSource, /\.schedule-map-pick\s*\{/);
});

test("multiple saved schedules share one active slot", () => {
  assert.match(appSource, /data-action="schedule-select"/);
  assert.match(appSource, /data-action="new-schedule"/);
  assert.match(appSource, /data-action="delete-schedule"/);
  assert.match(appSource, /data-action="save-schedule"/);
  assert.match(appSource, /\/api\/schedule\/save/);
  assert.match(appSource, /scheduleId:\s*draft\.selectedId/);
  assert.match(appSource, /activeScheduleId/);
  assert.match(appSource, /make this the only active schedule/);
  assert.match(stylesSource, /\.schedule-library\s*\{/);
});

test("LLM itinerary controls are no longer exposed", () => {
  assert.doesNotMatch(appSource, /OPENAI_API_KEY/);
  assert.doesNotMatch(appSource, /\/api\/itineraries\/interpret/);
  assert.doesNotMatch(appSource, /PLAN FROM DESCRIPTION/);
});
