# ESP-32 smoker relay

Small Arduino/PlatformIO sketch that sits near the smoker, talks to it over Bluetooth, and serves the same temperature packet the dashboard already understands on a LAN HTTP endpoint.

The live dashboard default stays **This server** (the media-server radio). Enable **ESP-32 relay** in Settings only when you want the board to be the radio.

## What it serves (LAN only)

| Path | Notes |
|------|--------|
| `GET /api/reading` | Latest temps as JSON (`setPoint`, `grill`, `probeTargets`, `probes`, `rssi`, `wifiRssi`, `char`). `200` when a 20-byte NXE packet is cached, `503` while scanning. Unauthenticated (monitor poll). |
| `GET /api/gatt` | Cached service/characteristic dump (`r/w/n/i`, last-read hex). No Bluetooth addresses. Unauthenticated LAN diagnostic. |
| `GET /health` | Board status: `name`, STA IP, `wifiRssi`, `bleRssi`, `lastErr`, `packetChar`. No Bluetooth addresses. Unauthenticated (LAN picker poll). |
| `GET /` | SoftAP: house Wi-Fi + name + OTA token form. STA (LAN IP): **name only** (auth). House PSK is not accepted on the STA IP. |
| `POST /wifi` | SoftAP only. Saves name, house Wi-Fi, optional OTA token to NVS. `404` on STA. |
| `POST /name` | STA rename. Requires `X-Relay-Token` or form `token` (token lives in NVS, set on SoftAP, not git). |
| `POST /ota` | HTTP firmware update on LAN STA (SoftAP recovery). Auth required. Pauses BLE. Single-slot + USB recovery. Body cap ~1.5 MB. No ArduinoOTA UDP :3232. |

The HTTP server binds to the board's own AP/STA address on port 80. It is not a WAN service. The Python app will refuse to poll a non-LAN host.

## Network

**Default (no extra flags):** the board starts a local AP:

- SSID: `smoker-relay`
- SoftAP address: `192.168.4.1`

Settings → ESP-32 relay discovers boards on the house LAN via `/health` and shows **name — IP**. SoftAP `192.168.4.1` is only reachable from a client associated with the AP.

**House LAN from a phone:** join SoftAP `smoker-relay`, open `http://192.168.4.1/`, set a **relay name**, house SSID, password, and OTA token. Those stay in NVS (not git). After STA joins, the LAN IP serves a **name-only** form (`POST /name`, token required). `POST /wifi` is 404 on that IP so the house PSK is not sitting on the STA address. Settings → ESP-32 relay lists name + IP — pick it.

Do not UniFi-forward the SoftAP, `192.168.1.118`, or port 80. Do not put house Wi-Fi or the OTA token in git or `platformio.ini`.

**OTA:** `curl -H 'X-Relay-Token: <nvs>' -F 'firmware=@firmware.bin' http://<sta-ip>/ota`. Token is minted into NVS on first boot (USB serial prints it once) or set on SoftAP. `GET /health` and `GET /api/reading` stay unauthenticated. Pause BLE during OTA; NVS is not erased. First cut is single-slot; USB `pio run -t upload` is the recovery path.

SoftAP PSK lives in `secrets.h` (gitignored). Copy `secrets.example.h` to `secrets.h` before a local build. Do not commit it. The flashed board already has its PSK in firmware; this change is repo-only until the next flash.

## Build (do not flash during a live cook)

The smoker allows one Bluetooth connection. Flashing or running this board against a cook that is already on the media-server radio will steal that connection.

```bash
pio run
```

To upload later, when you intend to switch:

```bash
pio run -t upload
```

`upload` overwrites firmware only. Do **not** run `pio run -t erase` or esptool erase — house Wi-Fi and relay name live in NVS and should survive a flash. SoftAP redo is only if STA comes up with no LAN IP.

Crash-fix (2026-08-27): do not call `scan->stop()` from the NimBLE advertise callback (LoadProhibited). Stop from `loop()` instead. STA-only when NVS has Wi-Fi (AP+STA + NimBLE aborts). Do not call `WiFi.setSleep(false)` with BLE on. Null-adv guard; delay scan until after BLE init. OTA de-inits NimBLE first.

## Packet

Same decoder as `server.py` `decode_packet`: reject `len < 20`; little-endian u16 setpoint @ 4, grill @ 6, probe targets @ 8/10, probes @ 16/18.

`0000cc01-…` **READ** on this grill is 14 ASCII bytes of the grill LAN IP, not the temp packet (bb01 is the IP char; cc01 currently duplicates it). Temps come from a **notify** (or indicate) on whichever characteristic carries the 20-byte NXE frame. The firmware subscribes every `canNotify`/`canIndicate` characteristic (CCCD only — no protocol value writes, no AT-02 `55 AA`). `/api/reading.char` and `/health.packetChar` name the UUID that produced the last valid packet. USB `pio run -t upload` only (do not erase NVS).
