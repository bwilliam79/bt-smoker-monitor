# BT Smoker Monitor

A local web dashboard for monitoring a Nexgrill Bluetooth smoker in real time. A Python backend polls the smoker via BLE every N seconds and pushes live temperature data to any browser via WebSocket — no cloud, no app required.

![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue) ![License: MIT](https://img.shields.io/badge/license-MIT-green)

---

## Features

- **Auto-discovery** — finds the smoker by scanning for BLE devices with the NXE prefix; retries automatically when out of range
- **Live temperature dashboard** — smoker temp, up to 2 meat probes
- **Trending chart** — 24-hour scrolling history of all temps with target lines
- **Server-side ETA** — estimated time remaining per probe, computed server-side via linear regression over the full probe history so all connected clients see the same value instantly
- **Stall detection** — detects when a probe temp stalls (< 2°F movement in 20 min), excludes the stall period from the regression to keep the rate accurate, and shows *"In stall – ETA paused"*
- **At / Over / Under temperature alerts** — visual indicators with colour coding and pulsing animation
- **Audible alarms** — siren when a probe exceeds its target; voice announcement when a probe reaches its target
- **Push notifications** — optional ntfy.sh integration for probe at temp, probe over temp, grill at temp, grill over temp, smoker connected, and smoker disconnected events
- **Smoker at-temperature timer** — shows how long the smoker has held its set temperature
- **Offline detection** — dashboard reflects when the smoker is unreachable; cards hide automatically
- **Multi-client** — open the dashboard in multiple browsers simultaneously; all receive the same server-computed state
- **In-app settings** — gear icon (⚙️) opens a settings modal to choose This server vs ESP-32 relay, pick a LAN relay (name + IP) or a Bluetooth adapter, and set the ntfy.sh topic
- **Runs as a Docker container**
- **App icon** — favicon, Apple touch icon, and PWA icons use a head-on photo of the smoker lid (wood handle). Hopper artwork is not used.

---

## Requirements

- Python 3.12+ **or** Docker
- A Bluetooth adapter (built-in or USB)
- Nexgrill smoker (tested with NXE-13CB970)
- Any modern browser for the dashboard

---

## Docker (recommended)

Pre-built images for `amd64`, `arm64`, and `armv7` are published to GitHub Container Registry on every push.

### Run

```bash
docker run -d \
  --name smoker \
  --restart unless-stopped \
  --net=host \
  --privileged \
  -v /var/run/dbus:/var/run/dbus \
  -v /path/to/bt-smoker-monitor/data:/data \
  ghcr.io/bwilliam79/bt-smoker-monitor:latest \
  --port 8080
```

Then open **http://\<host-ip\>:8080** in any browser.

> **Tip:** Replace `/path/to/bt-smoker-monitor/data` with a directory on your host (e.g. `/home/user/docker/bt-smoker-monitor/data`). This is where the app persists its ntfy topic across container restarts.

### Bluetooth flags explained

| Flag | Why it's needed |
|------|----------------|
| `--net=host` | Lets Bleak scan for BLE advertisements on the host's radio |
| `--privileged` | Grants the container access to host Bluetooth hardware |
| `-v /var/run/dbus:/var/run/dbus` | Gives the container access to the host's `bluetoothd` via D-Bus |
| `-v ...:/data` | Persists settings (ntfy topic, adapter choice) across container restarts |

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | `8080` | Web server port |
| `--interval` | `30` | BLE poll interval in seconds |
| `--address` | *(auto)* | Hardcode a BLE address to skip scanning |
| `--adapter` | *(auto)* | Initial Bluetooth adapter (e.g. `hci1`). Can also be changed at runtime via the in-app settings UI. |
| `--ntfy-topic` | *(none)* | ntfy.sh topic for push notifications. Can also be set via `NTFY_TOPIC` env var or the in-app settings UI |
| `--debug` | off | Enable verbose poll logging |

---

## Configuration

### In-app settings (⚙️)

Click the **⚙️** icon in the top-right corner to open the settings modal.

| Setting | Description |
|---------|-------------|
| **CONNECTION** | **This server** (default) talks to the smoker from the media-server radio. **ESP-32 relay** hides the adapter dropdown; the smoker talks to the ESP-32 instead. Save commits the whole modal. The switch takes effect on the next poll. |
| **Bluetooth Adapter** | Shown for This server. Select which adapter to use. Lists available adapters with their id and friendly name. Change takes effect on the next scan. |
| **Relay** | Shown for ESP-32 relay. Scans the LAN for boards that answer `GET /health` and lists **name — IP** (name from the SoftAP Wi-Fi form). Pick one — happy path is pick, not type. If discovery finds nothing, a secondary **Or type IP** field appears. Public / WAN hosts are rejected. Live boards without a name field still appear as `smoker-relay` + IP. |
| **ntfy.sh Topic** | Push notification topic. Leave blank to disable. |

Settings are saved to `/data/config.json` and persist across container restarts. Missing `connection` means This server, so a live cook keeps using the media-server radio.

### ESP-32 relay

Optional second radio: an ESP-32 near the smoker serves `GET /api/reading` on its LAN address. Join its SoftAP (`smoker-relay`) and set a **relay name** plus house Wi-Fi at `http://192.168.4.1/` (saved on the board, not in git). Settings → ESP-32 relay scans the house LAN `/24` for `/health` (not Docker bridge networks) and lets you **pick** name + IP; typing an IP is only a fallback when discovery finds nothing. Live boards without a name field still appear as `smoker-relay` + IP until reflashed. The dashboard still runs in this same app — there is no second instance. Build notes and the LAN-only API are in [`firmware/esp32-relay/README.md`](firmware/esp32-relay/README.md). Do not flash the board during a live cook; the smoker allows one Bluetooth connection.

---

## Push Notifications

The app uses [ntfy.sh](https://ntfy.sh) — a free, open-source push notification service — to send alerts to your phone.

### Setup

1. Install the **ntfy** app on your phone ([iOS](https://apps.apple.com/app/ntfy/id1625641461) / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy))
2. Subscribe to your chosen topic in the ntfy app
3. Enter the same topic in the app's settings modal (⚙️)

### Notification events

| Event | Priority |
|-------|----------|
| Probe at temperature | High |
| Probe over temperature | Urgent |
| Grill at set point | Default |
| Grill over temperature | Urgent |
| Smoker connected | Default |
| Smoker disconnected | Default |

---

## Dashboard

| Element | Description |
|---------|-------------|
| **⚙️ button** | Opens the settings modal (top-right corner) |
| **Status bar** | WebSocket connection and BLE smoker status |
| **Smoker card** (blue) | Current grill temp, set point, at-temp timer, over/under temp indicator |
| **Probe 1 card** (red) | Current probe temp, target, ETA — hidden when probe not connected |
| **Probe 2 card** (yellow) | Current probe temp, target, ETA — hidden when probe not connected |
| **Chart** | 24-hour scrolling history with dashed target lines |
| **Log panel** | Connection events, alarms, and setting changes |

### Temperature indicators

| State | Display |
|-------|---------|
| Heating toward target | ETA ~23 min (server-computed, updates every poll) |
| Temp stalled | In stall – ETA paused |
| Within 5°F of target | At Temperature |
| More than 5°F over target | **Over Temperature** (pulsing border) |
| More than 5°F under target | **Under Temperature** |

### Alarms

- **Probe reaches target** — voice announcement + push notification. Re-fires if probe drops 5°F below target and climbs back.
- **Probe over temperature** — siren repeating every 30 seconds + push notification. Clears automatically when temp drops back within range.
- **Smoker over temperature** — same behaviour as probes.

> **Note:** Browsers require a user interaction before playing audio. Tap/click the page once after loading to ensure alarms sound.

---

## Bluetooth Notes

The smoker only supports **one BLE connection at a time**. Close the official Nexgrill app before using this monitor, otherwise connections will fail.

If you have multiple Bluetooth adapters, select the one to use from the in-app settings (⚙️) while Connection is This server. You can also set the initial adapter at startup with `--adapter hciX`.

---

## Versioning

The app version is stored in `VERSION` (`MAJOR.MINOR.PATCH`) and shown in the footer of the page. Bump patch for bug fixes, minor for enhancements/features, major for breaking changes.

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `bleak` | Cross-platform BLE client |
| `fastapi` | Async web framework |
| `uvicorn` | ASGI server |
| `websockets` | WebSocket support for uvicorn |
