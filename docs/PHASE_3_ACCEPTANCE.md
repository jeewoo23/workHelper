# Phase 3 acceptance

Phase 3's implementation is complete. This checklist separates deterministic
automated verification from the final actions that must be observed on the
tethered iPhone.

## Automated verification

Run from the repository root:

```bash
uv run pytest -q
node --test tests/frontend_*.test.js
```

The 2026-07-30 implementation pass completed with 63 Python tests and 10
frontend behavior tests passing.

Covered behavior includes:

- GPX upload, validation, persistent listing, detail, and deletion.
- Route-aware, source, and uniform timing modes.
- Strict half-second phone-track resampling and registry persistence, including
  dense and duplicate source timestamps.
- Coordinate-to-road route generation with provider ETA.
- Full Google Maps directions URL parsing.
- Safe Google-owned short-link expansion boundaries.
- Actionable rejection of named-place links that require Phase 4 geocoding.
- Persistent provider, ETA, endpoint, and source metadata.
- Close-coordinate map framing and simultaneous left-rail sections.
- Validated static-coordinate activation, status reporting, and clearing.

## Tethered-iPhone verification

1. Start the controller with `./scripts/run_frontend.sh`, connect and unlock the
   iPhone, and confirm the top banner reports the device.
2. Open **Build Route**, enter two coordinates, generate a preview, and confirm
   the route line and exact requested endpoint pins are distinct.
3. Paste a coordinate-based Google Maps directions link and press **Load
   Link**. Confirm the saved card says **Google Maps** and shows provider ETA.
4. Refresh the page. Confirm the same card still identifies its Google Maps
   source, provider, ETA, labels, and endpoints.
5. Select the route, keep **Use Best Available Travel Time** enabled, and press
   **Prepare**. Confirm it becomes phone-ready.
6. Press **Start on Phone** and watch a short portion on the iPhone. Confirm
   position changes continuously and the simulated marker follows the map
   route.
7. Pause for at least 30 seconds, resume, and confirm progress does not jump
   during the pause.
8. Stop midway. Confirm the owned playback process exits and real location is
   restored. If necessary, press **Clear Location** once.
9. Delete the generated import and confirm both its saved source and prepared
   route disappear from the library.
10. Re-run L1 → L2 and L2 → L1 briefly to confirm the bundled routes were not
    regressed, then stop and clear location.

Phase 3 is accepted when every physical-device step above is observed on the
target phone.
