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

test("route builder exposes the Google Maps link workflow", () => {
  assert.match(appSource, /data-builder-input="google-maps-url"/);
  assert.match(appSource, /data-action="generate-google-maps"/);
  assert.match(appSource, /\/api\/routes\/from-google-maps-link/);
  assert.match(appSource, /google_maps_geocoding_required/);
  assert.match(stylesSource, /\.builder-link\s*\{/);
});

test("generated Google Maps routes retain their source label", () => {
  assert.match(
    appSource,
    /metadata\.sourceType === "google-maps"\s*\?\s*"Google Maps"/
  );
  assert.match(appSource, /metadata\.requestedOrigin/);
  assert.match(appSource, /metadata\.requestedDestination/);
});
