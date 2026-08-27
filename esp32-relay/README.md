# ESP-32 smoker relay

Small Arduino/PlatformIO sketch that sits near the smoker, talks to it over Bluetooth, and serves the same temperature packet the dashboard already understands on a LAN HTTP endpoint.

The live dashboard default stays **This server** (the media-server radio). Enable **ESP-32 relay** in Settings only when you want the board to be the radio.

## What it serves (LAN only)

| Path | Notes |
|------|--------|
| `GET /api/reading` | Latest temps as JSON (`setPoint`, `grill`, `probeTargets`, `probes`, `rssi`). `200` when a packet is cached, `503` while scanning. |
| `GET /health` | Board status. No Bluetooth addresses. |
| `GET /` | `{"ok":true,"service":"smoker-ble-relay"}` |

The HTTP server binds to the board's own AP/STA address on port 80. It is not a WAN service. The Python app will refuse to poll a non-LAN host.

## Network

**Default (no extra flags):** the board starts a local AP:

- SSID: `smoker-relay`
- SoftAP address: `192.168.4.1`

Enter that address in Settings → ESP-32 relay → Relay host. This is only reachable from a client associated with the AP.

**From media-server on the house LAN:** rebuild with station credentials as *local* build flags (do not commit them):

```ini
build_flags =
    -D RELAY_AP_SSID=\"smoker-relay\"
    -D RELAY_AP_PASS=\"nxe-relay-32\"
    -D RELAY_STA_SSID=\"your-lan-ssid\"
    -D RELAY_STA_PASS=\"your-lan-pass\"
```

Then put the board's DHCP address in the Relay host field.

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
