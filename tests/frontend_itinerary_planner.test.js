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

test("itinerary planner uses an explicit review and confirmation flow", () => {
  assert.match(appSource, /data-itinerary-input="description"/);
  assert.match(appSource, /\/api\/itineraries\/interpret/);
  assert.match(appSource, /\/api\/routes\/from-itinerary/);
  assert.match(appSource, /data-itinerary-confirm/);
  assert.match(appSource, /class="itinerary-timeline"/);
  assert.match(appSource, /confirmed:\s*true/);
  assert.match(appSource, /Confirm every resolved place/);
});

test("itinerary planner explains configuration and sparse generation", () => {
  assert.match(appSource, /OPENAI_API_KEY/);
  assert.match(appSource, /Stationary hours stay sparse/);
  assert.match(appSource, /metadata\.sourceType === "llm-itinerary"/);
  assert.match(stylesSource, /\.itinerary-review\s*\{/);
});
