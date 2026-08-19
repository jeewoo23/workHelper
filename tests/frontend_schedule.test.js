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

test("LLM itinerary controls are no longer exposed", () => {
  assert.doesNotMatch(appSource, /OPENAI_API_KEY/);
  assert.doesNotMatch(appSource, /\/api\/itineraries\/interpret/);
  assert.doesNotMatch(appSource, /PLAN FROM DESCRIPTION/);
});
