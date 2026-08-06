(function exposeRouteProgress(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.RouteProgress = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createRouteProgress() {
  function timeValue(point) {
    return point.time instanceof Date
      ? point.time.getTime()
      : new Date(point.time).getTime();
  }

  function partitionTimedRoute(points, progress) {
    if (!points.length) {
      return { current: { lat: 0, lon: 0 }, completed: [], remaining: [] };
    }

    const startTime = timeValue(points[0]);
    const endTime = timeValue(points.at(-1));
    const clampedProgress = Math.min(1, Math.max(0, Number(progress) || 0));
    const targetTime = startTime + Math.max(0, endTime - startTime) * clampedProgress;
    const nextIndex = points.findIndex(point => timeValue(point) >= targetTime);

    let current;
    if (nextIndex <= 0) {
      current = { lat: points[0].lat, lon: points[0].lon };
    } else if (nextIndex < 0) {
      current = { lat: points.at(-1).lat, lon: points.at(-1).lon };
    } else {
      const first = points[nextIndex - 1];
      const second = points[nextIndex];
      const firstTime = timeValue(first);
      const span = timeValue(second) - firstTime;
      const localProgress = span > 0 ? (targetTime - firstTime) / span : 1;
      current = {
        lat: first.lat + (second.lat - first.lat) * localProgress,
        lon: first.lon + (second.lon - first.lon) * localProgress
      };
    }

    const completed = points.filter(point => timeValue(point) < targetTime);
    const completedPath = [...completed, current];
    if (completedPath.length === 1) completedPath.unshift(points[0]);

    return {
      current,
      completed: completedPath,
      remaining: [
        current,
        ...points.filter(point => timeValue(point) > targetTime)
      ]
    };
  }

  function playbackRateMultiplier(devicePlaybackActive, previewSpeed) {
    return devicePlaybackActive ? 1 : previewSpeed;
  }

  return {
    partitionTimedRoute,
    playbackRateMultiplier
  };
}));
