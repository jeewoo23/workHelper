const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function extractFunction(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `${name} must exist`);
  const openingBrace = source.indexOf("{", start);
  let depth = 0;
  for (let index = openingBrace; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") depth -= 1;
    if (depth === 0) return source.slice(start, index + 1);
  }
  throw new Error(`${name} has no closing brace`);
}

const appSource = fs.readFileSync(
  path.join(__dirname, "../frontend/app.js"),
  "utf-8"
);
const stylesSource = fs.readFileSync(
  path.join(__dirname, "../frontend/styles.css"),
  "utf-8"
);

test("nearby coordinates remain visually separated when the map frames them", () => {
  const points = [
    { lat: 37.4000000, lon: -122.1000000 },
    { lat: 37.4000500, lon: -122.0999500 }
  ];
  let framedBounds;
  const context = {
    route: () => points,
    state: {
      viewer: {
        isDestroyed: () => false,
        camera: {
          setView: ({ destination }) => {
            framedBounds = destination;
          }
        },
        scene: { requestRender() {} }
      }
    },
    Cesium: {
      Rectangle: {
        fromDegrees: (west, south, east, north) => ({
          west,
          south,
          east,
          north
        })
      }
    }
  };
  vm.runInNewContext(extractFunction(appSource, "frameRoute"), context);

  context.frameRoute(false);

  const coordinateSpan = points[1].lat - points[0].lat;
  const framedSpan = framedBounds.north - framedBounds.south;
  assert.ok(
    coordinateSpan / framedSpan >= 0.2,
    `nearby pins occupy only ${(coordinateSpan / framedSpan * 100).toFixed(1)}% of the framed map`
  );
});

test("opening one left-rail section does not close the others", () => {
  assert.doesNotMatch(
    appSource,
    /candidate !== section\)\s*candidate\.open = false/
  );
});

test("the whole left rail owns vertical scrolling", () => {
  const sidebarRule = stylesSource.match(
    /\.sidebar-accordions\s*\{([\s\S]*?)\}/
  );
  assert.ok(sidebarRule, "sidebar accordion styles must exist");
  assert.match(sidebarRule[1], /overflow-y:\s*auto/);
});

test("desktop layout gives more room to functionality than the map-first layout", () => {
  assert.match(
    stylesSource,
    /grid-template-columns:\s*360px minmax\(400px, 1fr\) 330px/
  );
  assert.match(
    stylesSource,
    /grid-template-columns:\s*300px minmax\(340px, 1fr\) 290px/
  );
});

test("playback rail can collapse into a wider functionality rail", () => {
  assert.match(appSource, /data-action="collapse-control-panel"/);
  assert.match(appSource, /data-action="expand-control-panel"/);
  assert.match(appSource, /function setControlPanelCollapsed\(collapsed\)/);
  assert.match(stylesSource, /\.operations-grid\.control-panel-collapsed/);
  assert.match(stylesSource, /grid-template-columns:\s*702px minmax\(400px, 1fr\)/);
});

test("functionality rails use a larger readable type scale", () => {
  assert.match(stylesSource, /\.route-panel :is\(input, button, select, textarea\)/);
  assert.match(stylesSource, /\.control-panel :is\(input, button, select, textarea\)/);
  assert.match(stylesSource, /font-size:\s*14px/);
});
