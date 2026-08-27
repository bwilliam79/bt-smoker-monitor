# ESP-32 smoker relay

Small Arduino/PlatformIO sketch that sits near the smoker, talks to it over Bluetooth, and serves the same temperature packet the dashboard already understands on a LAN HTTP endpoint.

The live dashboard default stays **This server** (the media-server radio). Enable **ESP-32 relay** in Settings only when you want the board to be the radio.

## What it serves (LAN only)

| Path | Notes |
|------|--------|
| `GET /api/reading` | Latest temps as JSON (`setPoint`, `grill`, `probeTargets`, `probes`, `rssi`). `200` when a packet is cached, `503` while scanning. |
| `GET /health` | Board status including SoftAP-set `name` and STA IP. No Bluetooth addresses. |
| `GET /` | Phone form: relay name + house Wi-Fi. SoftAP stays up. Name and credentials persist in NVS (not git). |

The HTTP server binds to the board's own AP/STA address on port 80. It is not a WAN service. The Python app will refuse to poll a non-LAN host.

## Network

**Default (no extra flags):** the board starts a local AP:

- SSID: `smoker-relay`
- SoftAP address: `192.168.4.1`

Settings → ESP-32 relay discovers boards on the house LAN via `/health` and shows **name — IP**. SoftAP `192.168.4.1` is only reachable from a client associated with the AP.

**House LAN from a phone:** join SoftAP `smoker-relay`, open `http://192.168.4.1/`, set a **relay name**, house SSID, and password. Name and credentials persist in NVS on the board (not git). The form shows the DHCP address when STA joins. After STA joins, Settings → ESP-32 relay should list that name and IP — pick it (type IP only if discovery finds nothing). Live boards without the name field still appear as `smoker-relay` + IP until reflashed.

Do not UniFi-forward the SoftAP. Do not put house Wi-Fi in git or `platformio.ini`.

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

## Packet

Same NXE prefix and `0000cc01-…` characteristic as `server.py`: little-endian u16 setpoint @ 4, grill @ 6, probe targets @ 8/10, probes @ 16/18.
