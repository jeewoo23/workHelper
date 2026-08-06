# Physical iPad compatibility exploration

**Status:** Research and implementation plan; no application code changed  
**Date:** August 5, 2026  
**Current device dependency:** `pymobiledevice3` 9.40.0

## Executive conclusion

Central Blue can reasonably support a physically tethered iPad without a new
route format or playback engine. `pymobiledevice3` explicitly supports iPhone
and iPad, and its iOS/iPadOS 17+ location implementation sends latitude and
longitude through the device-wide DVT LocationSimulation service. The service
implementation does not contain an iPhone-only model check. The existing GPX
track preparation, timing, process lifecycle, pause/resume, map, and route UI
can therefore remain shared.

That conclusion is **expected compatibility, not yet physical-device proof**.
The exact iPad must still pass the static-set, route-play, stop, and clear tests
below. The most important application change is not the word “iPad”; it is
making device identity explicit. Central Blue currently displays the first
device returned by discovery and invokes location commands without a UDID, so
the UI and the process can refer to different devices when an iPhone and iPad
are both visible.

Recommended first scope:

- macOS host;
- one explicitly selected iPhone or iPad;
- USB transport only;
- iOS/iPadOS 17.4 or later;
- Developer Mode enabled and the required developer image mounted;
- the currently locked `pymobiledevice3` 9.40.0 until the iPad smoke test is
  complete.

Wi-Fi transport, iOS/iPadOS below 17, and multi-device simultaneous playback
should remain separate follow-up work.

## Evidence that the playback mechanism is portable to iPad

The upstream project describes itself as supporting iOS devices including
“iPhone, iPad, ...” and exposes device families such as `iPhone` and `iPad`
through the same lockdown abstraction. Its short device record includes
`DeviceClass`, `DeviceName`, `ProductVersion`, `ProductType`, UDID, and
connection type. See the [`pymobiledevice3` 9.40.0 README][pmd-readme],
[`DeviceClass` and `short_info`][pmd-lockdown], and the
[`usbmux list` implementation][pmd-usbmux].

For iOS 17 and newer, the CLI's `set`, `play`, and `clear` commands all create
the same DVT `LocationSimulation` service. That service invokes
`simulateLocationWithLatitude:longitude:` and `stopLocationSimulation`; it does
not branch on device family. GPX playback iterates track points, sleeps for the
timestamp delta, and sends every point through the same `set` method. See the
[`simulate-location` CLI][pmd-cli-location],
[`LocationSimulation` service][pmd-location-service], and
[`GPX playback loop][pmd-location-base].

Apple documents physical iPad as a normal Xcode development target, Developer
Mode for both iOS and iPadOS, and the same cable trust process for iPhone and
iPad. See [running on physical devices][apple-physical-devices],
[enabling Developer Mode][apple-developer-mode], and
[Trust This Computer][apple-trust].

Taken together, these are strong reasons to expect the existing location
protocol to work on iPad. They do not replace a test on the intended iPad model
and iPadOS build, because this is an undocumented Apple developer service being
used through a third-party implementation.

## Current application audit

### What already works unchanged

- GPX parsing, timing, interpolation, and route-aware timing are independent of
  device family.
- The process manager uses the modern
  `developer dvt simulate-location set|play|clear` command family.
- Static location deliberately retains the child process, matching
  `pymobiledevice3`'s behavior of waiting after a successful `set`.
- Pause and resume signal the host playback process and therefore do not depend
  on iPhone screen dimensions or hardware.
- The frontend runs on the Mac. Supporting iPad does not require an iPad-sized
  layout unless the project later intends to open the controller itself in
  Safari on the iPad.

### Functional gaps

1. **Discovery is not actually USB-only.**
   [`environment.py`](../src/route_controller/environment.py) runs
   `pymobiledevice3 usbmux list` without `--usb`, even though that upstream
   command lists both USB and Wi-Fi devices. A paired device can therefore be
   counted even when it is not tethered. The empty-state text nevertheless says
   “No USB-connected iPhone found.”

2. **There is no selected device.**
   [`app.js`](../frontend/app.js) parses the raw JSON string and displays the
   first record. [`playback.py`](../src/route_controller/playback.py) does not
   add `--udid`, and upstream defaults to the first USB device. With an iPhone
   and an iPad connected, the UI can show one while the command targets the
   other. The same risk applies to static set, route play, and clear.

3. **The backend returns command output instead of a device model.**
   `/api/status` sends an `EnvironmentReport` containing
   `device_probe_output`; the browser is responsible for JSON parsing and
   selection. That makes selection rules, USB filtering, validation, and error
   reporting hard to enforce in one place.

4. **Tunnel policy is fixed.**
   `PlaybackManager` defaults to `userspace=True` for every command. That is the
   best no-root path for a current USB iPad, but it is not a complete support
   policy for every iPadOS version.

5. **The command path assumes OS 17 or newer.**
   Central Blue always builds `developer dvt simulate-location` commands.
   `pymobiledevice3` documents a different
   `developer simulate-location` command for versions below 17 in its
   [DVT CLI recipes][pmd-dvt-recipes]. Either declare 17+ as the minimum or select the
   command family from `ProductVersion`.

6. **User-facing copy is iPhone-specific.**
   CLI help, errors, backend status, button labels, transport titles, and setup
   documentation say “iPhone,” “phone,” and “iOS.” Examples include
   [`server.py`](../src/route_controller/server.py),
   [`app.js`](../frontend/app.js), [`README.md`](../README.md), and
   [`BUILD_SPEC.md`](BUILD_SPEC.md). Most should say “device,” while the live
   card should derive “iPhone” or “iPad” from `DeviceClass`. The OS label should
   be “OS” or dynamically “iOS”/“iPadOS.”

7. **The package identity is iPhone-specific.**
   [`pyproject.toml`](../pyproject.toml) names the project
   `iphone-route-controller`, and generated GPX currently uses “iPhone Route
   Controller” as its creator. Those strings do not block playback but should
   be generalized before presenting iPad as a supported target.

## Setup requirements for a tethered iPad

### Cable, trust, and Developer Mode

Use a cable that carries data, not a charge-only cable. Apple says a connected
iPhone or iPad should appear in Finder, and on macOS the user may need to allow
the accessory, unlock the device, and approve Trust This Computer. Apple also
documents that Developer Mode applies to iPadOS and appears under **Settings >
Privacy & Security > Developer Mode** after pairing has been initiated. Enabling
it requires a restart and passcode confirmation. See
[Apple's connection troubleshooting][apple-recognition],
[trust instructions][apple-trust], and
[Developer Mode instructions][apple-developer-mode].

If Developer Mode is absent or the device is not fully paired, use Xcode's
Device Hub with the cable connected. Apple documents cable pairing for physical
devices and surfaces device issues in that UI. See
[Managing devices in Device Hub][apple-device-hub].

“USB-connected” describes the physical attachment, not necessarily every
protocol hop. Apple's Xcode 15 connectivity technote says developer traffic to
an Apple device connected by USB uses a network-based interface and link-local
IPv6. VPN clients, packet filters, or security software can therefore break
developer-service access even while Finder still sees the iPad. See
[Apple TN3158][apple-tn3158]. Central Blue should distinguish “USB device not
found” from “device found, developer tunnel unavailable” instead of collapsing
both into a disconnected state.

### Developer image

`pymobiledevice3` groups location simulation under `developer`, and its official
CLI guide pairs Developer Mode with mounting the Developer Disk Image or
personalized image:

```bash
uv run pymobiledevice3 mounter auto-mount
```

See the upstream [Developer Mode and DDI recipe][pmd-recipes]. Mounting may
require network access to obtain the matching image. This should be treated as
a setup/health-check step rather than retried blindly for every route.

### iPadOS 17+ tunnel behavior

The repository currently locks `pymobiledevice3` 9.40.0. Its tagged tunnel
guide says `--userspace` creates a no-root, in-process tunnel; it is rebuilt per
command and works for host-initiated DVT services. For 17.4+, it uses the
CoreDeviceProxy lockdown service over USB. For 17.0–17.3.1, the path involves
RemotePairing over Bonjour/Wi-Fi, so it should not be treated as equivalent to
the straightforward 17.4+ USB path. See the
[`pymobiledevice3` 9.40 tunnel guide][pmd-940-tunnels].

Current upstream documentation has since made the no-root userspace tunnel the
default for 17.4+ and routes 17.0–17.3.1 to privileged `tunneld` by default
because the no-root path is Wi-Fi-only and unreliable on macOS. See the
[current tunnel guide][pmd-current-tunnels]. This is relevant design guidance,
but Central Blue must not assume current `master` behavior while its dependency
constraint is `pymobiledevice3>=9,<10` and its lock file contains 9.40.0.

For the first iPad release:

| iPadOS version and transport | Proposed support | Reason |
| --- | --- | --- |
| 17.4+ over USB | Supported target | Same no-root userspace DVT path already used for iPhone |
| 17.0–17.3.1 over USB | Compatibility mode | Prefer a documented `tunneld` path; do not force userspace blindly |
| Below 17 | Out of first scope | Requires the legacy non-DVT command family |
| Wi-Fi-only transport | Out of first scope | Requires explicit network discovery/pairing and different failure handling |

The application should parse `ProductVersion` and expose the selected tunnel
strategy in status. It should never run `sudo` itself. If privileged `tunneld`
is required, show the exact user-run command and wait for the user to start it.

### Wi-Fi transport is a separate feature

The app's current product promise is a tethered device, so discovery should run:

```bash
uv run pymobiledevice3 usbmux list --usb
```

Upstream documents that plain `usbmux list` includes USB and Wi-Fi, while
`--usb` and `--network` filter those transports. Developer commands also expose
`--mobdev2` for Bonjour/network discovery and `--udid` for selection. Supporting
Wi-Fi later therefore requires an explicit transport choice, pairing state,
same-network handling, and reconnect behavior; it should not happen
accidentally because a network record appeared in the USB device list.

## iPad hardware considerations

Apple says GPS is available on iPhone and on **iPad Wi-Fi + Cellular** models.
An iPad without cellular can still use Wi-Fi and Bluetooth-derived Location
Services, but its restored real position can be less precise. See
[Apple's Location Services explanation][apple-location].

This distinction should not affect the simulated coordinate itself: the DVT
service accepts coordinates and does not inspect hardware GPS capability. That
is an inference from the upstream implementation and must be verified on the
target iPad. It does affect what happens after **Clear Location**:

- Wi-Fi + Cellular iPad: expected to restore GPS-backed real location when
  Location Services and reception permit it.
- Wi-Fi-only iPad: expected to restore Wi-Fi/Bluetooth-derived location, which
  may be coarse or unavailable.

Central Blue's **Mac Physical Location** remains the browser's Mac location.
It is not the iPad's physical location and should retain that label regardless
of the connected device.

## Recommended implementation

### 1. Add a device inventory and selected-device model

Move discovery/parsing out of the frontend and return structured records from
the backend. A useful shape is:

```json
{
  "devices": [
    {
      "id": "full-udid-kept-local",
      "displayId": "00008110…A1B2",
      "name": "Jeewoo's iPad",
      "deviceClass": "iPad",
      "productType": "iPad…",
      "productVersion": "18.6",
      "osName": "iPadOS",
      "connectionType": "USB"
    }
  ],
  "selectedDeviceId": "full-udid-kept-local",
  "selectionRequired": false
}
```

Rules:

1. Discover with `usbmux list --usb` for the tethered MVP.
2. Accept `DeviceClass` values `iPhone` and `iPad`; report other classes as
   unsupported rather than controlling them.
3. Auto-select only when exactly one supported USB device exists.
4. When multiple devices exist, require an explicit selection in the UI.
5. Persist the chosen UDID locally, but revalidate it on every refresh.
6. Display only a shortened identifier in ordinary UI/logs.
7. Refuse set/play/clear if the selected device disappeared or changed
   transport.

### 2. Target every device command by UDID

Extend the argument builders for set, play, and clear with an optional UDID and
pass `--udid <selected-id>` to all three. Upstream 9.40.0 reads `--udid` for the
userspace tunnel and otherwise defaults to the first USB device; see its
[`cli_common.py` device dependency][pmd-cli-common].

The selected UDID belongs in `PlaybackManager` state. Capture it in
`ActivePlayback` and static-location state so a refresh can say which device is
being controlled and so a newly connected device cannot receive a stale Clear
command.

### 3. Add an OS-aware command/tunnel strategy

Create one small policy object based on selected device metadata, for example:

```text
DeviceTarget
├── udid
├── device class: iPhone | iPad
├── product version
├── transport: USB
├── command family: DVT | legacy
└── tunnel mode: userspace | tunneld
```

For the first release, reject unsupported combinations with an actionable
message. Do not silently fall back between devices or transports. If support
for versions below 17 is later added, keep route semantics the same and swap
only the upstream command family.

### 4. Generalize UI and recovery copy

Prefer “device” for actions and state:

- **Start on device**, **Pause device**, **Device transport**;
- **No USB device detected**;
- **Connect and unlock the iPad**, derived from `DeviceClass`;
- **iPadOS** for iPad and **iOS** for iPhone;
- **Device fixed at ...** instead of **iPhone fixed at ...**.

The header can show the actual device name and family. Error messages should
use the selected device noun and keep the same concrete recovery instructions
for trust, Developer Mode, DDI, and tunnel failures.

### 5. Keep dependency upgrades separate

As of this report the upstream latest release is
[`pymobiledevice3` 10.3.1][pmd-latest-release], while this project explicitly
requires major version 9 and locks 9.40.0. Major-version migration can change
CLI behavior, particularly automatic tunnel selection. First prove the iPad on
the locked version; then evaluate version 10 in a separate change with command
snapshot tests and physical-device smoke tests.

## Test plan

### Automated tests to add

Device discovery and selection:

- no devices, one USB iPad, one USB iPhone, and unsupported device class;
- one device represented over both USB and Network is not double-counted;
- iPhone and iPad connected together requires selection;
- a remembered UDID is selected only if still present over USB;
- device metadata maps `iPad` to `iPadOS` and `iPhone` to `iOS`;
- malformed `usbmux` JSON and partial records fail safely.

Command construction and lifecycle:

- set, play, and clear all include the same `--udid`;
- a userspace command targets the selected UDID;
- disconnecting the selected iPad blocks new commands;
- replacing a static location and starting a route retain the same device;
- stop/clear cannot switch to another newly connected first device;
- OS/tunnel policy covers 16.x, 17.0–17.3.1, and 17.4+ boundaries;
- friendly errors say iPad/iPadOS when the selected device is an iPad.

API/frontend:

- `/api/status` returns structured device inventory and selection state;
- the device selector appears only when needed;
- control buttons remain disabled until selection is unambiguous;
- iPad name, model, iPadOS version, and USB transport render correctly;
- no functional UI text still promises iPhone-only control.

### Physical iPad acceptance test

Record the iPad model, `ProductType`, iPadOS version, connection type, and
whether it is Wi-Fi-only or Wi-Fi + Cellular. Then:

1. Connect with a data-capable cable; approve the Mac accessory prompt and
   Trust This Computer if shown.
2. Confirm the iPad appears in Finder or Xcode Device Hub.
3. Enable Developer Mode, restart, and confirm it on the iPad.
4. Run `uv run pymobiledevice3 usbmux list --usb`; verify `DeviceClass` is
   `iPad`, `ConnectionType` is `USB`, and capture the UDID locally.
5. Run `uv run pymobiledevice3 mounter auto-mount` and resolve any developer
   image error before location testing.
6. Preview the exact UDID-targeted static command, then deliberately set a
   harmless test coordinate using `--userspace` on iPadOS 17.4+.
7. Verify Apple Maps and one test app receiving Core Location show the simulated
   point. A web site's IP geolocation is not a valid check.
8. Clear the simulation and verify the iPad returns to its best available real
   location; record expected precision differences for Wi-Fi-only hardware.
9. Play a short GPX route and verify movement cadence, endpoint, pause, resume,
   stop, and clear.
10. Disconnect during playback, reconnect, and verify the app reports the
    selected iPad as unavailable without targeting another device.
11. Connect an iPhone at the same time and prove every operation still targets
    the selected iPad.
12. Restart Central Blue after an interrupted static simulation and verify its
    recovery guidance restores real location on the same UDID.

Do not promote iPad support from “experimental” to “supported” until set, play,
stop, and clear pass on the intended physical iPad.

## Suggested delivery order

1. **Compatibility spike:** perform the direct `pymobiledevice3` static and
   short-route test on the actual iPad with the locked environment.
2. **Device targeting:** structured USB inventory, selected UDID, and UDID on
   every device command.
3. **iPad-aware product copy:** dynamic family/OS names and generalized docs.
4. **Acceptance hardening:** multi-device, disconnect, stale-selection, and
   Wi-Fi-only-real-location tests.
5. **Optional expansion:** legacy pre-17 command family, Wi-Fi transport, then a
   separately tested migration to `pymobiledevice3` 10.

The first two steps are the minimum needed to say that Central Blue controls an
iPad intentionally rather than merely hoping the first connected device is the
iPad.

## Sources

Primary sources only were used for this report.

- Apple Developer: [Enabling Developer Mode on a device][apple-developer-mode]
- Apple Developer: [Running your app on simulated or physical devices][apple-physical-devices]
- Apple Developer: [Managing your simulated and physical devices in Device Hub][apple-device-hub]
- Apple Developer: [TN3158 — Resolving Xcode 15 device connection issues][apple-tn3158]
- Apple Support: [About the Trust This Computer alert][apple-trust]
- Apple Support: [If your computer doesn't recognize your iPhone or iPad][apple-recognition]
- Apple Support: [About privacy and Location Services in iOS and iPadOS][apple-location]
- `pymobiledevice3` 9.40.0: [README][pmd-readme], [Developer Mode/DDI recipe][pmd-recipes], [DVT location recipes][pmd-dvt-recipes], and [iOS 17+ tunnel guide][pmd-940-tunnels]
- `pymobiledevice3` source: [device dependency][pmd-cli-common], [USB/network discovery][pmd-usbmux], [DVT location CLI][pmd-cli-location], [DVT location service][pmd-location-service], and [GPX loop][pmd-location-base]
- `pymobiledevice3`: [current tunnel guide][pmd-current-tunnels] and [10.3.1 release][pmd-latest-release]

[apple-developer-mode]: https://developer.apple.com/documentation/xcode/enabling-developer-mode-on-a-device
[apple-physical-devices]: https://developer.apple.com/documentation/xcode/running-your-app-on-simulated-or-physical-devices
[apple-device-hub]: https://developer.apple.com/documentation/xcode/pairing-your-devices-with-your-mac
[apple-tn3158]: https://developer.apple.com/documentation/technotes/tn3158-resolving-xcode-15-device-connection-issues
[apple-trust]: https://support.apple.com/109054
[apple-recognition]: https://support.apple.com/108643
[apple-location]: https://support.apple.com/102515
[pmd-readme]: https://github.com/doronz88/pymobiledevice3/blob/v9.40.0/README.md#pymobiledevice3
[pmd-recipes]: https://github.com/doronz88/pymobiledevice3/blob/v9.40.0/docs/guides/cli-recipes.md#developer-mode-and-ddi
[pmd-dvt-recipes]: https://github.com/doronz88/pymobiledevice3/blob/v9.40.0/docs/guides/cli-recipes.md#dvt-examples
[pmd-940-tunnels]: https://github.com/doronz88/pymobiledevice3/blob/v9.40.0/docs/guides/ios17-tunnels.md
[pmd-current-tunnels]: https://github.com/doronz88/pymobiledevice3/blob/master/docs/guides/ios17-tunnels.md
[pmd-cli-common]: https://github.com/doronz88/pymobiledevice3/blob/v9.40.0/pymobiledevice3/cli/cli_common.py#L190-L340
[pmd-usbmux]: https://github.com/doronz88/pymobiledevice3/blob/v9.40.0/pymobiledevice3/cli/usbmux.py#L78-L132
[pmd-cli-location]: https://github.com/doronz88/pymobiledevice3/blob/v9.40.0/pymobiledevice3/cli/developer/dvt/simulate_location.py#L11-L59
[pmd-location-service]: https://github.com/doronz88/pymobiledevice3/blob/v9.40.0/pymobiledevice3/services/dvt/instruments/location_simulation.py#L6-L46
[pmd-location-base]: https://github.com/doronz88/pymobiledevice3/blob/v9.40.0/pymobiledevice3/services/dvt/instruments/location_simulation_base.py#L21-L44
[pmd-lockdown]: https://github.com/doronz88/pymobiledevice3/blob/v9.40.0/pymobiledevice3/lockdown.py#L77-L84
[pmd-latest-release]: https://github.com/doronz88/pymobiledevice3/releases/tag/v10.3.1
