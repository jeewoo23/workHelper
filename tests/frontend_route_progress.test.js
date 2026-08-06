const assert = require("node:assert/strict");
const test = require("node:test");

const {
  partitionTimedRoute,
  playbackRateMultiplier
} = require("../frontend/route-progress.js");

function point(lat, lon, seconds) {
  return {
    lat,
    lon,
    time: new Date(Date.UTC(2026, 0, 1, 12, 0, seconds))
  };
}

test("completed and remaining route geometry only meet at the current point", () => {
  const points = [
    point(37.0, -122.0, 0),
    point(37.1, -122.1, 10),
    point(37.2, -122.2, 20)
  ];

  const partition = partitionTimedRoute(points, 0.25);

  assert.deepEqual(partition.current, {
    lat: 37.05,
    lon: -122.05
  });
  assert.deepEqual(partition.completed, [
    points[0],
    partition.current
  ]);
  assert.deepEqual(partition.remaining, [
    partition.current,
    points[1],
    points[2]
  ]);
});

test("phone playback advances at real time instead of preview speed", () => {
  assert.equal(playbackRateMultiplier(true, 10), 1);
  assert.equal(playbackRateMultiplier(false, 10), 10);
});
