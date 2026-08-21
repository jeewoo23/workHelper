const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const appSource = fs.readFileSync(
  path.join(__dirname, "../frontend/app.js"),
  "utf-8"
);
const stylesSource = fs.readFileSync(
  path.join(__dirname, "../frontend/styles.css"),
  "utf-8"
);

test("coordinates panel exposes editable static-position controls", () => {
  assert.match(appSource, /data-input="static-latitude"/);
  assert.match(appSource, /data-input="static-longitude"/);
  assert.match(appSource, /data-action="pick-static-location"/);
  assert.match(appSource, /data-action="activate-location">ACTIVATE/);
  assert.match(stylesSource, /\.static-location-form\s*\{/);
  assert.match(stylesSource, /\.static-location-actions\s*\{/);
});

test("activate sends a validated coordinate to the local controller", () => {
  assert.match(appSource, /supportsBackendCapability\("staticLocation"\)/);
  assert.match(appSource, /\/api\/location\/set/);
  assert.match(appSource, /Latitude must be between -90 and 90/);
  assert.match(appSource, /Longitude must be between -180 and 180/);
});

test("static position displays persistent-session health and recovery state", () => {
  assert.match(appSource, /healthCheckIntervalSeconds/);
  assert.match(appSource, /sessionMode/);
  assert.match(appSource, /lastReassertedAt/);
  assert.match(appSource, /Reconnecting to the device/);
  assert.match(appSource, /persistent session/);
  assert.match(appSource, /simulatedLocation\.error\?\.message/);
});

test("map selection fills a pending target without activating the phone", () => {
  assert.match(appSource, /state\.staticLocation\.pickMode/);
  assert.match(appSource, /Cesium\.ScreenSpaceEventType\.LEFT_CLICK/);
  assert.match(appSource, /state\.staticLocation\.draftPoint = point/);
  assert.match(appSource, /syncStaticLocationTargetMarker\(\)/);
  assert.doesNotMatch(
    appSource,
    /if \(staticPick\)[\s\S]{0,900}apiRequest\("\/api\/location\/set"/
  );
});
