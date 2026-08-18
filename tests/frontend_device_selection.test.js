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

test("device header supports explicit USB device selection", () => {
  assert.match(appSource, /data-action="device-select"/);
  assert.match(appSource, /\/api\/device\/select/);
  assert.match(appSource, /report\.selectedDevice/);
  assert.match(appSource, /device\.deviceClass/);
  assert.match(appSource, /device\.osName/);
  assert.match(stylesSource, /\.device-selector\s*\{/);
});

test("functional device copy includes iPad and is not iPhone-only", () => {
  assert.match(appSource, /No USB iPhone or iPad detected/);
  assert.match(appSource, /connect an iPhone or iPad to play/i);
  assert.doesNotMatch(appSource, /IPHONE PLAYBACK/);
  assert.doesNotMatch(appSource, /connected iPhone\.<\/em>/);
});
