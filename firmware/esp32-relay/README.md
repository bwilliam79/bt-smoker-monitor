# ESP-32 smoker relay

Small Arduino/PlatformIO sketch that sits near the smoker, talks to it over Bluetooth, and serves the same temperature packet the dashboard already understands on a LAN HTTP endpoint.

The live dashboard default stays **This server** (the media-server radio). Enable **ESP-32 relay** in Settings only when you want the board to be the radio.

## What it serves (LAN only)

| Path | Notes |
|------|--------|
| `GET /api/reading` | Latest temps as JSON (`setPoint`, `grill`, `probeTargets`, `probes`, `rssi`). `200` when a packet is cached, `503` while scanning. |
| `GET /health` | Board status. No Bluetooth addresses. |
| `GET /` | Phone Wi-Fi form (join house SSID). SoftAP stays up. |

The HTTP server binds to the board's own AP/STA address on port 80. It is not a WAN service. The Python app will refuse to poll a non-LAN host.

## Network

**Default (no extra flags):** the board starts a local AP:

- SSID: `smoker-relay`
- SoftAP address: `192.168.4.1`

Enter that address in Settings → ESP-32 relay → Relay host. This is only reachable from a client associated with the AP.

**House LAN from a phone:** join SoftAP `smoker-relay`, open `http://192.168.4.1/`, enter house SSID and password. Credentials persist in NVS on the board (not git). The form shows the DHCP address when STA joins. Put that address in Settings → ESP-32 relay → Relay host.

Do not UniFi-forward the SoftAP. Do not put house Wi-Fi in git or `platformio.ini`.

The AP PSK in `platformio.ini` is a local device password for the board's own AP, not a cloud credential.

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
