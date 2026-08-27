"""
BT Smoker Monitor — BLE poller + WebSocket server
Usage: python3 server.py [--interval 30] [--port 8080]
"""
import asyncio
import json
import logging
import argparse
import math
import os
import time
from pathlib import Path
import hmac
import ipaddress
import re
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler, ProxyHandler
from urllib.error import HTTPError
from concurrent.futures import ThreadPoolExecutor, as_completed
import socket

import subprocess

from bleak import BleakScanner, BleakClient, BleakError
from bleak.exc import BleakDBusError
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request as FastAPIRequest, Response, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

import session_auth

# ── BLE UUIDs ────────────────────────────────────────────────────────────────
CHAR_IP   = '0000bb01-0000-1000-8000-00805f9b34fb'
CHAR_TEMP = '0000cc01-0000-1000-8000-00805f9b34fb'

PROBE_DISCONNECTED = 999
TARGET_PREFIX      = 'NXE'
HISTORY_MAX_AGE    = 24 * 60 * 60   # seconds
CONFIG_PATH        = Path('/data/config.json')

# ── ETA / stall constants ─────────────────────────────────────────────────────
STALL_WINDOW_SECS = 20 * 60   # how far back to look for a stall (20 min)
STALL_THRESHOLD_F = 2.0       # °F range within stall window that counts as stalled
PROBE_HISTORY_MAX_AGE_SECS = 4 * 60 * 60   # keep last 4h of probe points for ETA regression
PROBE_HISTORY_STALE_SECS   = 10 * 60       # drop probe_history if newest point is older than this on reconnect

# ── BLE reconnect backoff ────────────────────────────────────────────────────
BACKOFF_START_SECS = 5
BACKOFF_MAX_SECS   = 60

# ── WebSocket idle cleanup ───────────────────────────────────────────────────
WS_IDLE_TIMEOUT_SECS = 120   # drop connections with no pong/activity for this long
WS_REAPER_INTERVAL   = 30    # how often to sweep stale clients

# ── Auth ─────────────────────────────────────────────────────────────────────
AUTH_TOKEN = os.environ.get('AUTH_TOKEN', '').strip() or None

# ── CORS ─────────────────────────────────────────────────────────────────────
_default_origins = 'http://localhost:8080,http://127.0.0.1:8080,http://localhost:8888,http://127.0.0.1:8888,https://smoker.tehkernel.com'
CORS_ORIGINS = [o.strip() for o in os.environ.get('CORS_ORIGINS', _default_origins).split(',') if o.strip()]

log = logging.getLogger('smoker')

# ── Packet decoder ────────────────────────────────────────────────────────────
# ── Pure helpers (unit-tested) ──────────────────────────────────────
def read_u16_le(data: bytes, offset: int) -> int:
    return data[offset] | (data[offset + 1] << 8)

def decode_packet(data: bytes):
    if len(data) < 20:
        return None
    return {
        'setPoint':     read_u16_le(data, 4),
        'grill':        read_u16_le(data, 6),
        'probeTargets': [read_u16_le(data, 8),  read_u16_le(data, 10)],
        'probes':       [read_u16_le(data, 16), read_u16_le(data, 18)],
    }


# ── ESP-32 relay (LAN HTTP poller) ────────────────────────────────────────────
CONNECTION_LOCAL = 'local'
CONNECTION_RELAY = 'relay'
DEFAULT_RELAY_HOST = '192.168.4.1'
_SINGLE_LABEL = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$')
_adapter_names: dict[str, str] = {}


def _host_is_lan(host: str) -> bool:
    """True for private / loopback / link-local IPs, *.local, or a single DNS label."""
    host = (host or '').strip().strip('[]')
    if not host:
        return False
    lowered = host.lower()
    if lowered.endswith('.local'):
        label = lowered[:-6]
        return bool(label) and bool(_SINGLE_LABEL.match(label))
    if _SINGLE_LABEL.match(host):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local) and not ip.is_unspecified and not ip.is_multicast


def parse_relay_host(raw: str) -> tuple[str, int] | None:
    """Return (host, port) if *raw* is a LAN-only relay address, else None.

    Accepts '192.168.4.1', '192.168.4.1:80', 'smoker-relay.local', or an
    accidental http://192.168.4.1 URL. Rejects public IPs and public DNS.
    """
    raw = (raw or '').strip()
    if not raw:
        return None
    port = 80
    if '://' in raw:
        parsed = urlparse(raw)
        if parsed.scheme != 'http':
            return None
        host = parsed.hostname or ''
        port = parsed.port or 80
    elif raw.startswith('['):
        try:
            bracket, rest = raw.split(']', 1)
            host = bracket[1:]
            if rest.startswith(':'):
                port = int(rest[1:])
        except (ValueError, IndexError):
            return None
    elif raw.count(':') == 1:
        host, _, port_s = raw.partition(':')
        try:
            port = int(port_s)
        except ValueError:
            return None
    else:
        host = raw
    if not host or not (1 <= port <= 65535):
        return None
    if not _host_is_lan(host):
        return None
    return host, port


def relay_reading_url(raw_host: str) -> str | None:
    parsed = parse_relay_host(raw_host)
    if not parsed:
        return None
    host, port = parsed
    try:
        ip = ipaddress.ip_address(host)
        if ip.version == 6:
            netloc = f'[{host}]:{port}' if port != 80 else f'[{host}]'
        else:
            netloc = f'{host}:{port}' if port != 80 else host
    except ValueError:
        netloc = f'{host}:{port}' if port != 80 else host
    return f'http://{netloc}/api/reading'


def relay_health_url(raw_host: str) -> str | None:
    parsed = parse_relay_host(raw_host)
    if not parsed:
        return None
    host, port = parsed
    try:
        ip = ipaddress.ip_address(host)
        if ip.version == 6:
            netloc = f'[{host}]:{port}' if port != 80 else f'[{host}]'
        else:
            netloc = f'{host}:{port}' if port != 80 else host
    except ValueError:
        netloc = f'{host}:{port}' if port != 80 else host
    return f'http://{netloc}/health'


def sanitize_relay_display_name(raw) -> str:
    """SoftAP-set relay label for UI. No Bluetooth names/addresses."""
    if not isinstance(raw, str):
        return 'smoker-relay'
    cleaned = ''.join(c for c in raw.strip() if 32 <= ord(c) < 127 and c not in '"\'<>\\&')
    cleaned = cleaned.strip()[:32]
    return cleaned or 'smoker-relay'


def parse_relay_health(payload: dict, probed_host: str) -> dict | None:
    """Map ESP-32 /health JSON to {name, host}. Works with or without name field.

    Live boards before the SoftAP-name flash still expose ok/ap/sta/haveReading;
    those appear as smoker-relay + IP. No Bluetooth address fields are returned.
    """
    if not isinstance(payload, dict) or not payload.get('ok'):
        return None
    # Fingerprint the relay health shape (avoid random LAN HTTP services).
    if not any(k in payload for k in ('haveReading', 'ap', 'sta')):
        return None
    # Prefer STA IP when present and LAN; else the probed host.
    host = None
    sta = payload.get('sta')
    if isinstance(sta, str) and sta.strip() and parse_relay_host(sta.strip()):
        host = normalize_relay_host(sta.strip())
    if not host:
        host = normalize_relay_host(probed_host)
    if not host:
        return None
    name = sanitize_relay_display_name(payload.get('name'))
    return {'name': name, 'host': host}


# Docker user-defined bridges (172.16/12) and libvirt virbr0. Expanding those
# /24s hangs Settings → Scan on a host that also runs other stacks.
_SKIP_SCAN_NETS = (
    ipaddress.ip_network('172.16.0.0/12'),
    ipaddress.ip_network('192.168.122.0/24'),
)


def lan_scan_source_ok(raw: str) -> bool:
    """True if this host IP's /24 should be probed for ESP-32 relays."""
    try:
        ip = ipaddress.ip_address((raw or '').strip())
    except ValueError:
        return False
    if ip.version != 4 or not ip.is_private or ip.is_loopback:
        return False
    return not any(ip in net for net in _SKIP_SCAN_NETS)


def discovery_probe_hosts(local_ipv4s: list[str], extra_hosts: list[str] | None = None) -> list[str]:
    """Build unique LAN IPv4 probe list from house /24s plus extras (saved host, etc.)."""
    hosts: list[str] = []
    seen: set[str] = set()

    def add(h: str):
        h = (h or '').strip()
        if not h or h in seen:
            return
        try:
            ip = ipaddress.ip_address(h)
        except ValueError:
            return
        if ip.version != 4:
            return
        if not (ip.is_private or ip.is_loopback or ip.is_link_local):
            return
        if ip.is_unspecified or ip.is_multicast:
            return
        seen.add(h)
        hosts.append(h)

    for raw in extra_hosts or []:
        parsed = parse_relay_host(raw)
        if parsed:
            add(parsed[0])

    for addr in local_ipv4s or []:
        if not lan_scan_source_ok(addr):
            continue
        ip = ipaddress.ip_address(addr)
        net = ipaddress.ip_network(f'{ip}/24', strict=False)
        for host_ip in net.hosts():
            add(str(host_ip))
    return hosts


def parse_relay_telemetry(payload: dict) -> dict | None:
    """Map ESP-32 /health JSON to wifiRssi/bleRssi/lastErr. None if not a relay."""
    if not isinstance(payload, dict) or not payload.get('ok'):
        return None
    if not any(k in payload for k in ('haveReading', 'ap', 'sta', 'wifiRssi', 'bleRssi')):
        return None

    def as_rssi(v):
        if isinstance(v, bool) or not isinstance(v, int):
            return None
        if v > 0 or v < -120:
            return None
        return v

    err = payload.get('lastErr')
    if isinstance(err, str):
        err = ''.join(c for c in err if 32 <= ord(c) < 127)[:80]
    else:
        err = ''
    sta = payload.get('sta')
    sta_s = ''
    if isinstance(sta, str) and sta.strip() and parse_relay_host(sta.strip()):
        sta_s = sta.strip()
    name = sanitize_relay_display_name(payload.get('name'))
    return {
        'wifiRssi': as_rssi(payload.get('wifiRssi')),
        'bleRssi': as_rssi(payload.get('bleRssi')),
        'lastErr': err,
        'sta': sta_s,
        'name': name,
    }


def parse_relay_payload(payload: dict) -> dict | None:
    """Map ESP-32 /api/reading JSON onto decode_packet's dict. No address field."""
    if not isinstance(payload, dict) or not payload.get('ok'):
        return None
    try:
        targets = payload['probeTargets']
        probes = payload['probes']
        if not (isinstance(targets, list) and isinstance(probes, list)):
            return None
        if len(targets) < 2 or len(probes) < 2:
            return None
        return {
            'setPoint':     int(payload['setPoint']),
            'grill':        int(payload['grill']),
            'probeTargets': [int(targets[0]), int(targets[1])],
            'probes':       [int(probes[0]), int(probes[1])],
        }
    except (KeyError, TypeError, ValueError):
        return None


def normalize_relay_host(raw: str):
    """Canonical LAN host[:port], or DEFAULT_RELAY_HOST when blank. None if not LAN."""
    if not (raw or '').strip():
        return DEFAULT_RELAY_HOST
    parsed = parse_relay_host(raw)
    if not parsed:
        return None
    host, port = parsed
    return host if port == 80 else f'{host}:{port}'


def relay_host_is_allowed(raw: str) -> bool:
    return parse_relay_host(raw) is not None


reading_from_relay_payload = parse_relay_payload

# ── End pure helpers ──────────────────────────────────────────────────

def _local_ipv4_addrs() -> list[str]:
    """Best-effort private IPv4s on this host (for /24 LAN relay scan)."""
    found: set[str] = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            found.add(info[4][0])
    except Exception:
        pass
    try:
        # UDP connect does not send packets; reveals the outbound interface IP.
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('192.168.1.1', 80))
            found.add(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    try:
        out_h = subprocess.check_output(
            ['hostname', '-I'], text=True, timeout=1, stderr=subprocess.DEVNULL
        )
        for tok in out_h.split():
            found.add(tok.split('%')[0])
    except Exception:
        pass
    out = []
    for ip in sorted(found):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if lan_scan_source_ok(ip):
            out.append(ip)
    return out


def _http_get_json_lan(url: str, timeout: float = 0.45):
    """GET JSON from a LAN URL. No redirects, no proxy. Returns dict or None."""
    class _NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    req = Request(url, method='GET')
    req.add_header('User-Agent', 'bt-smoker-monitor-relay/1.0')
    req.add_header('Accept', 'application/json')
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read(4096)
    try:
        data = json.loads(raw.decode())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def discover_lan_relays(extra_hosts: list[str] | None = None) -> list[dict]:
    """Probe LAN /24s for ESP-32 /health. Returns [{name, host}, ...] sorted by name.

    Does not log Bluetooth addresses. Name comes from SoftAP-set /health.name when
    present; otherwise defaults to smoker-relay so pre-flash boards still appear.
    """
    probes = discovery_probe_hosts(_local_ipv4_addrs(), extra_hosts)
    if not probes:
        return []

    found: dict[str, dict] = {}

    def _probe(ip: str):
        url = relay_health_url(ip)
        if not url:
            return None
        try:
            payload = _http_get_json_lan(url)
        except Exception:
            return None
        return parse_relay_health(payload or {}, ip)

    # Bound concurrency so a full /24 finishes in a few seconds on a quiet LAN.
    with ThreadPoolExecutor(max_workers=64) as pool:
        futs = {pool.submit(_probe, ip): ip for ip in probes}
        for fut in as_completed(futs):
            hit = fut.result()
            if not hit:
                continue
            host = hit['host']
            prev = found.get(host)
            if not prev or (prev.get('name') == 'smoker-relay' and hit.get('name') != 'smoker-relay'):
                found[host] = hit

    return sorted(found.values(), key=lambda r: (r['name'].lower(), r['host']))


# ── ETA computation ───────────────────────────────────────────────────────────
def _linreg_rate(points: list[dict]) -> float | None:
    """Least-squares slope in °F/sec over a list of {temp, ts} dicts."""
    n = len(points)
    if n < 2:
        return None
    t0  = points[0]['ts']
    xs  = [p['ts'] - t0 for p in points]
    ys  = [p['temp']    for p in points]
    xm  = sum(xs) / n
    ym  = sum(ys) / n
    num = sum((xs[i] - xm) * (ys[i] - ym) for i in range(n))
    den = sum((xs[i] - xm) ** 2            for i in range(n))
    return (num / den) if den else None

def compute_probe_eta(history: list[dict], target: int, current_temp: int) -> tuple[int | None, bool]:
    """
    Return (eta_mins_or_None, stalled).

    Uses linear regression over all probe history since detection.
    Detects a stall when temp hasn't moved ≥ STALL_THRESHOLD_F in the last
    STALL_WINDOW_SECS, and excludes the stall period from the regression so
    the rate estimate reflects actual cooking progress.
    """
    if len(history) < 2 or target >= PROBE_DISCONNECTED or current_temp >= PROBE_DISCONNECTED:
        return None, False

    if current_temp >= target:
        return 0, False

    now    = history[-1]['ts']
    cutoff = now - STALL_WINDOW_SECS
    window = [p for p in history if p['ts'] >= cutoff]

    # Stall: need ≥3 points in the window and barely any temp movement
    stalled = (
        len(window) >= 3 and
        (max(p['temp'] for p in window) - min(p['temp'] for p in window)) < STALL_THRESHOLD_F
    )

    # Regression: exclude the flat stall window if stalling so it doesn't drag the slope down
    reg_pts = [p for p in history if p['ts'] < cutoff] if stalled else history
    if len(reg_pts) < 2:
        return None, stalled

    rate = _linreg_rate(reg_pts)   # °F / sec
    if rate is None or rate <= 0:
        return None, stalled

    secs = (target - current_temp) / rate
    return max(1, round(secs / 60)), stalled

# ── ntfy.sh push notifications ────────────────────────────────────────────────
def _ntfy_post(topic: str, title: str, message: str, priority: str, tags: str):
    try:
        # URL-encode topic so special chars like ?, #, /, etc. can't break the URL path.
        # urllib.request.urlopen uses HTTPS with TLS certificate verification by default
        # (via the system's SSL/TLS trust store). Explicit `verify` isn't a thing on
        # urlopen — if we ever switch to `requests`, add `verify=True`.
        safe_topic = quote(topic, safe='')
        req = Request(f'https://ntfy.sh/{safe_topic}', data=message.encode(), method='POST')
        req.add_header('Title', title)
        req.add_header('Priority', priority)
        req.add_header('User-Agent', 'bt-smoker-monitor/1.0')
        if tags:
            req.add_header('Tags', tags)
        with urlopen(req, timeout=5):
            pass
    except Exception:
        log.exception('ntfy notification failed')

async def notify(title: str, message: str, priority: str = 'default', tags: str = ''):
    topic = state.get('ntfy_topic')
    if not topic:
        return
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _ntfy_post, topic, title, message, priority, tags)

# ── App state ─────────────────────────────────────────────────────────────────
app      = FastAPI()
# Register CORS once — allow localhost by default, configurable via CORS_ORIGINS env var.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)
clients: dict = {}   # ws -> last_seen_ts
state    = {
    'last':          None,
    'smoker_online': False,
    'ip':            None,
    'address':       None,   # string address
    'rssi':          None,   # smoker BT (dongle adv or relay bleRssi)
    'wifiRssi':      None,   # ESP STA RSSI; relay mode only
    'bleRssi':       None,   # ESP↔smoker BT RSSI; relay mode only
    'lastErr':       '',     # ESP lastErr; relay mode only
    'relay_sta':     None,   # ESP STA IP from /health
    'relay_name':    '',
    'adapter':       None,
    'connection':    CONNECTION_LOCAL,  # 'local' (this server) | 'relay'
    'relay_host':    DEFAULT_RELAY_HOST,
    'history':       [],
    'log_history':   [],
    'interval':      30,
    'ntfy_topic':    None,
    'login_user':    '',
    'login_salt':    '',
    'login_hash':    '',
    # per-probe ETA state (reset when probe disconnects)
    'probe_history':    [[], []],    # [{temp, ts}, …] since probe first detected
    'probe_eta':        [None, None],  # minutes to target, or None
    'probe_stalled':    [False, False],
    # UI-set probe targets (override BLE targets when not None; persisted to config)
    'probe_ui_targets': [None, None],
    # notification dedup
    'notified': {
        'probe_at_temp':      [False, False],
        'probe_over_temp':    [False, False],
        'grill_at_temp':      False,
        'grill_over_temp':    False,
        'grill_under_temp':   False,
        'grill_reached_once': False,
    },
}

# ── Public-host session wall (LAN :8888 stays open) ──────────────────────────
_PUBLIC_OPEN = {('/login', 'GET'), ('/login', 'POST'), ('/favicon.svg', 'GET')}


def _is_https(request: FastAPIRequest) -> bool:
    proto = (request.headers.get('x-forwarded-proto') or request.url.scheme or '').split(',')[0].strip().lower()
    return proto == 'https'


@app.middleware('http')
async def auth_middleware(request: FastAPIRequest, call_next):
    path = request.url.path
    method = request.method.upper()
    if session_auth.host_is_public(request.headers.get('host')):
        if (path, method) not in _PUBLIC_OPEN and not session_auth.session_ok(request.headers.get('cookie')):
            if path.startswith('/api/') or path == '/ws':
                return JSONResponse({'error': 'Unauthorized'}, status_code=401)
            if method == 'GET' and path == '/':
                return RedirectResponse('/login', status_code=302)
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
        if path in ('/api/config', '/api/login') and method == 'POST':
            # Credential writes are LAN-only even with a public session.
            pass
    if AUTH_TOKEN and path.startswith('/api/'):
        if request.headers.get('X-Auth-Token') != AUTH_TOKEN:
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    return await call_next(request)

# ── WebSocket broadcast ───────────────────────────────────────────────────────
async def broadcast(msg: dict):
    if not clients:
        return
    text = json.dumps(msg)
    dead = []
    for ws in list(clients.keys()):
        try:
            await ws.send_text(text)
        except Exception:
            log.exception('WebSocket send failed; dropping client')
            dead.append(ws)
    for ws in dead:
        clients.pop(ws, None)

# ── Config file ───────────────────────────────────────────────────────────────
def load_config() -> dict:
    """Load ntfy_topic from CONFIG_PATH. Returns {} if missing or malformed.

    Values are coerced to strings so downstream code (which expects str) can't
    crash on an int/bool/None smuggled into the JSON file by hand-editing.
    """
    try:
        if CONFIG_PATH.exists():
            raw = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
            if not isinstance(raw, dict):
                log.warning('Config file is not a JSON object; ignoring')
                return {}
            out = {}
            for key in ('ntfy_topic', 'adapter', 'relay_host', 'login_user', 'login_salt', 'login_hash'):
                if key in raw and raw[key] is not None:
                    out[key] = str(raw[key]).strip()
            if 'connection' in raw and raw['connection'] is not None:
                conn = str(raw['connection']).strip().lower()
                if conn in (CONNECTION_LOCAL, CONNECTION_RELAY):
                    out['connection'] = conn
            if 'probe_targets' in raw and isinstance(raw['probe_targets'], list):
                out['probe_targets'] = raw['probe_targets']
            return out
    except Exception:
        log.exception('Could not read config file')
    return {}

def save_config(ntfy_topic: str, adapter: str = '', probe_targets: list | None = None,
                connection: str | None = None, relay_host: str | None = None) -> None:
    """Persist settings to CONFIG_PATH.

    Read-modify-write so unrelated keys added by future features (or by a
    hand-edit) aren't silently dropped on save.
    """
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
                if isinstance(raw, dict):
                    existing = raw
            except Exception:
                log.exception('Could not read existing config; will overwrite')
        existing['ntfy_topic'] = ntfy_topic
        if adapter:
            existing['adapter'] = adapter
        else:
            existing.pop('adapter', None)
        if probe_targets is not None:
            existing['probe_targets'] = probe_targets
        if connection in (CONNECTION_LOCAL, CONNECTION_RELAY):
            existing['connection'] = connection
        if relay_host is not None:
            existing['relay_host'] = relay_host
        CONFIG_PATH.write_text(json.dumps(existing, indent=2), encoding='utf-8')
    except Exception:
        log.exception('Could not write config file')


def save_login(username: str, password: str) -> None:
    """Persist hashed public-login credentials. Call on LAN only."""
    salt, hashed = session_auth.hash_password(password)
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
                if isinstance(raw, dict):
                    existing = raw
            except Exception:
                log.exception('Could not read existing config; will overwrite login keys')
        existing['login_user'] = username
        existing['login_salt'] = salt
        existing['login_hash'] = hashed
        CONFIG_PATH.write_text(json.dumps(existing, indent=2), encoding='utf-8')
        state['login_user'] = username
        state['login_salt'] = salt
        state['login_hash'] = hashed
    except Exception:
        log.exception('Could not write login config')

# ── Server-side log history ───────────────────────────────────────────────────
def add_log(tag: str, msg: str, cls: str, ts: float):
    state['log_history'].append({'tag': tag, 'msg': msg, 'cls': cls, 'ts': ts})
    cutoff = time.time() - HISTORY_MAX_AGE
    state['log_history'] = [e for e in state['log_history'] if e['ts'] >= cutoff]

# ── Clock-aligned sleep ───────────────────────────────────────────────────────
async def sleep_to_next_tick(interval: int) -> None:
    """Sleep until the next wall-clock multiple of *interval* seconds."""
    now   = time.time()
    delta = math.ceil(now / interval) * interval - now
    await asyncio.sleep(max(delta, 0.001))

# ── Notification checker ──────────────────────────────────────────────────────
async def check_notifications(dec: dict):
    # Defensive: never fire alarms based on a stale/synthesized reading. Today
    # only _process_reading calls this after a successful scan, but guard in
    # case a future caller feeds us a cached payload.
    if not state.get('smoker_online'):
        return
    probes   = dec['probes']
    targets  = dec['probeTargets']
    grill    = dec['grill']
    setpoint = dec['setPoint']
    n        = state['notified']

    # Reset grill "reached once" sticky flag when the smoker is powered down
    # (setpoint goes to 0) so the next cook starts fresh.
    if setpoint <= 0:
        n['grill_reached_once'] = False
        n['grill_under_temp']   = False

    # Grill at temp — skip when setpoint is 0 (smoker off / cooldown) so a cold
    # reading doesn't spuriously fire "at temperature".
    if setpoint > 0 and not n['grill_at_temp'] and abs(grill - setpoint) <= 5:
        n['grill_at_temp'] = True
        await notify('Smoker at Temperature', f'Smoker reached {setpoint}°F set point.', tags='fire')
    if n['grill_at_temp'] and grill < setpoint - 10:
        n['grill_at_temp'] = False

    # Grill reached-once sticky flag — used to arm the under-temp alert.
    if setpoint > 0 and abs(grill - setpoint) <= 5:
        n['grill_reached_once'] = True

    # Grill over temp — threshold is +25°F. Pellet smokers routinely swing a
    # few degrees as augers feed; alerting at +5 produces noise. Probe
    # over-temp stays at +5 (see below) because food going 5° past a meat
    # target means it's overdone.
    if setpoint > 0 and not n['grill_over_temp'] and grill > setpoint + 25:
        n['grill_over_temp'] = True
        await notify('Smoker Over Temperature', f'Smoker is {grill}°F — set point is {setpoint}°F.',
                     priority='urgent', tags='rotating_light')
    if n['grill_over_temp'] and grill <= setpoint + 25:
        n['grill_over_temp'] = False

    # Grill under temp — only arms after the smoker has reached set point, and
    # fires once when the reading drops >10° below. Resets when back within 5°.
    if (setpoint > 0 and n['grill_reached_once']
            and not n['grill_under_temp'] and grill < setpoint - 10):
        n['grill_under_temp'] = True
        await notify('Smoker Under Temperature',
                     f'Smoker dropped to {grill}°F — set point is {setpoint}°F.',
                     priority='high', tags='warning')
    if n['grill_under_temp'] and grill >= setpoint - 5:
        n['grill_under_temp'] = False

    # Per-probe (use UI-set target when available, fall back to BLE target)
    ui_targets = state['probe_ui_targets']
    for i, (temp, ble_target) in enumerate(zip(probes, targets)):
        target = ui_targets[i] if ui_targets[i] is not None else ble_target
        if temp >= PROBE_DISCONNECTED or target >= PROBE_DISCONNECTED:
            n['probe_at_temp'][i]   = False
            n['probe_over_temp'][i] = False
            continue

        if not n['probe_at_temp'][i] and temp >= target:
            n['probe_at_temp'][i] = True
            await notify(f'Probe {i + 1} at Temperature', f'Probe {i + 1} reached {target}°F.',
                         priority='high', tags='meat_on_bone')
        if n['probe_at_temp'][i] and temp < target - 5:
            n['probe_at_temp'][i] = False

        if not n['probe_over_temp'][i] and temp > target + 5:
            n['probe_over_temp'][i] = True
            await notify(f'Probe {i + 1} Over Temperature',
                         f'Probe {i + 1} is {temp}°F — target is {target}°F.',
                         priority='urgent', tags='rotating_light')
        if n['probe_over_temp'][i] and temp <= target + 5:
            n['probe_over_temp'][i] = False

# ── BLE polling loop ──────────────────────────────────────────────────────────
async def scan_and_read() -> tuple[dict | None, object | None, int | None]:
    """
    Scan for the smoker, connect, and read temperature.

    Uses BleakScanner.discover() so the scanner stops cleanly before we
    connect.  The BLEDevice object returned by discover() retains the BlueZ
    D-Bus path so BleakClient can connect without re-scanning.

    Critical: do NOT pass bluez={'adapter': …} to BleakClient.  The
    BLEDevice already carries the correct adapter path from the scanner;
    specifying it again triggers a different BlueZ connection path that
    causes le-connection-abort-by-local on Realtek adapters.

    Returns (decoded_packet, ble_device, rssi) or (None, None, None).
    """
    print(f'Scanning for smoker ({TARGET_PREFIX}*)…')
    scanner_kwargs = {'timeout': 15, 'scanning_mode': 'active'}
    if state['adapter']:
        scanner_kwargs['bluez'] = {'adapter': state['adapter']}

    discovered = await BleakScanner.discover(**scanner_kwargs, return_adv=True)
    found_pair = next(
        ((dev, adv) for dev, adv in discovered.values()
         if dev.name and dev.name.startswith(TARGET_PREFIX)),
        None,
    )
    if not found_pair:
        return None, None, None

    found_device, found_adv = found_pair
    found_rssi = found_adv.rssi
    print(f'Found: {found_device.name}  RSSI: {found_rssi} dBm')

    async with BleakClient(found_device, timeout=45) as client:
        # Always re-read IP on reconnect so we don't serve a stale address forever.
        try:
            raw_ip = await client.read_gatt_char(CHAR_IP)
            state['ip'] = ''.join(chr(b) for b in raw_ip if 32 <= b < 127)
            print(f'Smoker IP: {state["ip"]}')
        except Exception:
            log.exception('Failed to read smoker IP over GATT; leaving IP as-is')
        raw = await client.read_gatt_char(CHAR_TEMP)
        return decode_packet(bytes(raw)), found_device, found_rssi

def _relay_http_get(url: str, timeout: float = 8):
    """GET JSON from a LAN relay URL. No redirects, no proxy."""
    class _NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    req = Request(url, method='GET')
    req.add_header('User-Agent', 'bt-smoker-monitor-relay/1.0')
    req.add_header('Accept', 'application/json')
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


async def read_from_relay() -> tuple[dict | None, object | None, int | None]:
    """Poll ESP-32 /health (telemetry) then /api/reading (temps). Never follows a non-LAN host."""
    host = state.get('relay_host') or DEFAULT_RELAY_HOST
    health_url = relay_health_url(host)
    reading_url = relay_reading_url(host)
    if not health_url or not reading_url:
        log.warning('Relay host is not a LAN address; skipping poll')
        return None, None, None

    loop = asyncio.get_event_loop()
    try:
        health = await loop.run_in_executor(None, _relay_http_get, health_url)
        _apply_relay_telemetry(parse_relay_telemetry(health))
    except Exception:
        log.exception('Relay /health poll failed')

    try:
        payload = await loop.run_in_executor(None, _relay_http_get, reading_url)
    except HTTPError as exc:
        if exc.code != 503:
            log.exception('Relay poll failed')
        return None, None, None
    except Exception:
        log.exception('Relay poll failed')
        return None, None, None

    dec = parse_relay_payload(payload)
    if not dec:
        return None, None, None

    smoker_ip = payload.get('smokerIp')
    if isinstance(smoker_ip, str) and smoker_ip.strip():
        state['ip'] = smoker_ip.strip()

    rssi = payload.get('rssi')
    rssi_i = rssi if isinstance(rssi, int) else state.get('bleRssi')
    print(f'Relay reading  grill={dec["grill"]}°F  set={dec["setPoint"]}°F')
    return dec, None, rssi_i

async def _process_reading(dec: dict, tick_time: float, smoker_was_offline: bool, ble_device, rssi) -> bool:
    """Update state and broadcast a successful reading. Returns new smoker_was_offline value."""
    if smoker_was_offline:
        print('Smoker reconnected.')
        add_log('SYS', 'Smoker reconnected.', 'tag-sys', tick_time)
        await notify('Smoker Connected', 'Smoker monitor reconnected.', tags='white_check_mark')
        smoker_was_offline = False

    # Seed `notified` from the first reading after startup. Without this, a
    # container restart mid-cook resets `grill_reached_once` to False, which
    # silently disarms the under-temp alarm because that gate requires the
    # smoker to have been within 5°F of setpoint during this server session.
    # Also pre-mark currently at-temp / over-temp so we don't re-announce
    # state the user already saw before the restart.
    if not state.get('_seeded'):
        state['_seeded'] = True
        n  = state['notified']
        sp = dec['setPoint']
        gr = dec['grill']
        if sp > 0 and gr > 100:
            # Anything above warm room temp on a smoker that's actively
            # cooking (setpoint > 0) means the cook is in progress and we
            # should arm the under-temp alarm. False-positive risk on a
            # mid-ramp-up restart (grill 110°F, climbing toward setpoint)
            # is real but rare; missing a real drop is worse.
            n['grill_reached_once'] = True
            if abs(gr - sp) <= 5:
                n['grill_at_temp'] = True
            if gr > sp + 25:
                n['grill_over_temp'] = True
        # Probe at-temp / over-temp: pre-mark so a restart mid-cook with a
        # probe already past target doesn't re-announce on the next tick.
        ui_targets = state['probe_ui_targets']
        for i, (pt, bt) in enumerate(zip(dec['probes'], dec['probeTargets'])):
            tgt = ui_targets[i] if ui_targets[i] is not None else bt
            if pt >= PROBE_DISCONNECTED or tgt >= PROBE_DISCONNECTED:
                continue
            if pt >= tgt:
                n['probe_at_temp'][i] = True
            if pt > tgt + 5:
                n['probe_over_temp'][i] = True
        log.info(f'Seeded notification state from first reading: {n}')

    if ble_device is not None:
        state['address'] = ble_device.address
    elif (state.get('connection') or CONNECTION_LOCAL) == CONNECTION_RELAY:
        state['address'] = None
    if rssi is not None:
        state['rssi'] = rssi

    for i, (temp, ble_target) in enumerate(zip(dec['probes'], dec['probeTargets'])):
        eff_target = state['probe_ui_targets'][i] if state['probe_ui_targets'][i] is not None else ble_target
        if temp >= PROBE_DISCONNECTED:
            state['probe_history'][i].clear()
            state['probe_eta'][i]     = None
            state['probe_stalled'][i] = False
        else:
            ph = state['probe_history'][i]
            # If the smoker was offline long enough that the newest stored point
            # is stale (>10 min), drop it so ETA regression doesn't mix
            # pre-outage and post-outage points into a garbage slope.
            if ph and tick_time - ph[-1]['ts'] > PROBE_HISTORY_STALE_SECS:
                ph.clear()
            ph.append({'temp': temp, 'ts': tick_time})
            # Trim expired points with pop(0) in a tight loop — avoids the
            # O(n) list-comprehension rebuild we'd otherwise do every tick in
            # steady state.
            ph_cutoff = tick_time - PROBE_HISTORY_MAX_AGE_SECS
            while ph and ph[0]['ts'] < ph_cutoff:
                ph.pop(0)
            eta_mins, stalled = compute_probe_eta(ph, eff_target, temp)
            state['probe_eta'][i]     = eta_mins
            state['probe_stalled'][i] = stalled
            if stalled:
                log.info(f'Probe {i + 1} stall detected at {temp}°F')

    dec['ip']              = state['ip']
    dec['connection']      = state.get('connection') or CONNECTION_LOCAL
    if dec['connection'] == CONNECTION_RELAY:
        dec['adapter']     = None
        dec['relay_host']  = state.get('relay_host') or ''
        dec['address']     = None
    else:
        dec['adapter']     = _adapter_footer_label()
        dec['address']     = state['address']
    dec['rssi']            = state['rssi']
    dec['wifiRssi']        = state.get('wifiRssi')
    dec['bleRssi']         = state.get('bleRssi')
    dec['lastErr']         = state.get('lastErr') or ''
    dec['ts']              = tick_time
    dec['interval']        = state['interval']
    dec['eta']             = state['probe_eta'][:]
    dec['stalled']         = state['probe_stalled'][:]
    dec['probeUiTargets']  = state['probe_ui_targets'][:]
    dec['connected']       = True

    state['last'] = dec
    state['smoker_online'] = True
    state['history'].append(dec)
    cutoff = time.time() - HISTORY_MAX_AGE
    state['history'] = [p for p in state['history'] if p['ts'] >= cutoff]

    await broadcast(dec)
    await check_notifications(dec)

    probes_str = ', '.join(
        f'{p}°F → {dec["probeTargets"][i]}°F' if p < PROBE_DISCONNECTED else 'NC'
        for i, p in enumerate(dec['probes'])
    )
    log.info(f'Smoker: {dec["grill"]}°F  Set: {dec["setPoint"]}°F  Probes: [{probes_str}]')
    return smoker_was_offline

def _apply_relay_telemetry(tel: dict | None) -> None:
    """Store ESP /health radios. Never sets smoker_online / connected."""
    if not tel:
        return
    state['wifiRssi'] = tel.get('wifiRssi')
    state['bleRssi'] = tel.get('bleRssi')
    state['lastErr'] = tel.get('lastErr') or ''
    if tel.get('sta'):
        state['relay_sta'] = tel['sta']
    if tel.get('name'):
        state['relay_name'] = tel['name']
    if state['bleRssi'] is not None:
        state['rssi'] = state['bleRssi']


def _relay_telemetry_msg() -> dict:
    return {
        'rssi': state.get('rssi'),
        'wifiRssi': state.get('wifiRssi'),
        'bleRssi': state.get('bleRssi'),
        'lastErr': state.get('lastErr') or '',
        'connection': state.get('connection') or CONNECTION_LOCAL,
        'relay_host': state.get('relay_host') or '',
        'ip': state.get('ip'),
        'interval': state.get('interval'),
        'adapter': state.get('adapter'),
        'address': state.get('address'),
        'relay_name': state.get('relay_name') or '',
        'relay_sta': state.get('relay_sta'),
    }


def _mark_disconnected():
    """Clear stale temp/probe values and flag the cached 'last' payload as disconnected."""
    state['smoker_online'] = False
    relay = (state.get('connection') or CONNECTION_LOCAL) == CONNECTION_RELAY
    if not relay:
        state['rssi'] = None
        state['wifiRssi'] = None
        state['bleRssi'] = None
        state['lastErr'] = ''
        state['ip'] = None   # will be re-read on reconnect
    state['probe_eta']     = [None, None]
    state['probe_stalled'] = [False, False]
    # Rebuild state['last'] as a new dict — the previous dict is shared with the last
    # entry in state['history'] (see _process_reading), so mutating it in place would
    # retroactively corrupt historical data.
    if state['last'] is not None:
        state['last'] = {
            **state['last'],
            'connected': False,
            'grill':     None,
            'probes':    [None, None],
            'rssi':      state.get('rssi'),
            'wifiRssi':  state.get('wifiRssi'),
            'bleRssi':   state.get('bleRssi'),
            'lastErr':   state.get('lastErr') or '',
            'eta':       [None, None],
            'stalled':   [False, False],
        }

def _reset_bt_adapter(adapter: str | None) -> None:
    """Reset the BT adapter via hciconfig to clear a stuck BlueZ scan."""
    hci = adapter or 'hci0'
    try:
        subprocess.run(['hciconfig', hci, 'reset'], check=True, timeout=10,
                       capture_output=True)
        print(f'Adapter {hci} reset successfully.')
    except Exception:
        log.exception(f'Failed to reset adapter {hci}')


async def poll_loop(interval: int):
    smoker_was_offline = False
    backoff = BACKOFF_START_SECS
    consecutive_inprogress = 0
    INPROGRESS_RESET_THRESHOLD = 3
    await sleep_to_next_tick(interval)
    print(f'Polling aligned — first tick at {time.strftime("%H:%M:%S")}')
    while True:
        tick_time = math.floor(time.time() / interval) * interval

        success = False
        try:
            if (state.get('connection') or CONNECTION_LOCAL) == CONNECTION_RELAY:
                dec, ble_device, rssi = await read_from_relay()
            else:
                dec, ble_device, rssi = await scan_and_read()
            consecutive_inprogress = 0

            if dec:
                smoker_was_offline = await _process_reading(dec, tick_time, smoker_was_offline, ble_device, rssi)
                success = True
                backoff = BACKOFF_START_SECS   # reset backoff on any good read
            else:
                # Scan found nothing
                _mark_disconnected()
                await broadcast({'smoker_offline': True, 'connected': False, **_relay_telemetry_msg()})
                if not smoker_was_offline:
                    print('Smoker not found — will keep retrying.')
                    add_log('WARN', 'Smoker offline — retrying…', 'tag-warn', time.time())
                    await notify('Smoker Disconnected', 'Lost connection to smoker. Retrying…', tags='warning')
                    smoker_was_offline = True

        except BleakDBusError as exc:
            if 'InProgress' in str(exc):
                consecutive_inprogress += 1
                log.warning('BlueZ scan stuck InProgress (%d/%d)',
                            consecutive_inprogress, INPROGRESS_RESET_THRESHOLD)
                if consecutive_inprogress >= INPROGRESS_RESET_THRESHOLD:
                    log.warning('Resetting BT adapter to clear stuck scan…')
                    await asyncio.get_event_loop().run_in_executor(
                        None, _reset_bt_adapter, state.get('adapter'))
                    consecutive_inprogress = 0
                    await asyncio.sleep(3)   # give BlueZ a moment to settle
            else:
                consecutive_inprogress = 0
                log.exception('BLE D-Bus error during scan/read')
            _mark_disconnected()
            await broadcast({'smoker_offline': True, 'connected': False, **_relay_telemetry_msg()})
            if not smoker_was_offline:
                print('Smoker unreachable — will keep retrying.')
                add_log('WARN', 'Smoker unreachable — retrying…', 'tag-warn', time.time())
                await notify('Smoker Disconnected', 'Lost connection to smoker. Retrying…', tags='warning')
                smoker_was_offline = True
        except (BleakError, asyncio.TimeoutError):
            # bleak can raise asyncio.TimeoutError (not BleakError) on scan or
            # connect timeouts — treat both as routine "smoker out of range".
            consecutive_inprogress = 0
            log.exception('BLE error/timeout during scan/read')
            _mark_disconnected()
            await broadcast({'smoker_offline': True, 'connected': False, **_relay_telemetry_msg()})
            if not smoker_was_offline:
                print('Smoker unreachable — will keep retrying.')
                add_log('WARN', 'Smoker unreachable — retrying…', 'tag-warn', time.time())
                await notify('Smoker Disconnected', 'Lost connection to smoker. Retrying…', tags='warning')
                smoker_was_offline = True
        except Exception:
            consecutive_inprogress = 0
            log.exception('Unexpected error in poll loop')
            _mark_disconnected()
            await broadcast({'smoker_offline': True, 'connected': False, **_relay_telemetry_msg()})
            if not smoker_was_offline:
                print('Smoker unreachable — will keep retrying.')
                add_log('WARN', 'Smoker unreachable — retrying…', 'tag-warn', time.time())
                await notify('Smoker Disconnected', 'Lost connection to smoker. Retrying…', tags='warning')
                smoker_was_offline = True

        if success:
            # Clock-aligned sleep until the next poll tick
            sleep_for = tick_time + interval - time.time()
            await asyncio.sleep(max(sleep_for, 0.001))
        else:
            # Exponential backoff when we can't reach the smoker — avoid thrashing the adapter
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_SECS)

# ── HTTP / WebSocket routes ───────────────────────────────────────────────────
LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Smoker Monitor — Sign in</title>
<style>
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         background:#140c08; color:#f5e6d3; font-family:system-ui,sans-serif; }
  form { width:min(22rem,92vw); background:#1f140e; border:1px solid #3d2a1d; border-radius:12px; padding:1.5rem; }
  h1 { margin:0 0 1rem; font-size:1.15rem; color:#f97316; }
  label { display:block; font-size:.75rem; letter-spacing:.04em; color:#c4a484; margin:.7rem 0 .25rem; }
  input { width:100%; box-sizing:border-box; padding:.55rem .7rem; border-radius:8px; border:1px solid #5a3c28;
          background:#140c08; color:#f5e6d3; }
  button { margin-top:1.1rem; width:100%; padding:.7rem; border:0; border-radius:8px; background:#f97316; color:#1a0e08; font-weight:600; cursor:pointer; }
  p { color:#c4a484; font-size:.85rem; }
</style>
</head>
<body>
<form method="POST" action="/login">
  <h1>Smoker Monitor</h1>
  __MSG__
  <label for="username">Username</label>
  <input id="username" name="username" type="text" autocomplete="username" required>
  <label for="password">Password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  <button type="submit">Sign in</button>
</form>
</body>
</html>
"""


@app.get('/')
async def index():
    html = Path('index.html').read_text(encoding='utf-8')
    version = Path('VERSION').read_text(encoding='utf-8').strip()
    return HTMLResponse(html.replace('{{VERSION}}', version))


@app.get('/login')
async def login_get(request: FastAPIRequest):
    msg = ''
    if request.query_params.get('err') == '1':
        msg = '<p>Wrong username or password.</p>'
    elif not (state.get('login_user') and state.get('login_hash')):
        msg = '<p>Set a username and password on LAN :8888 first (gear → Public login).</p>'
    return HTMLResponse(LOGIN_PAGE.replace('__MSG__', msg))


@app.post('/login')
async def login_post(request: FastAPIRequest, username: str = Form(''), password: str = Form('')):
    stored_user = state.get('login_user') or ''
    salt = state.get('login_salt') or ''
    hashed = state.get('login_hash') or ''
    user_ok = hmac.compare_digest(username.strip(), stored_user) if stored_user else False
    pass_ok = session_auth.verify_password(password, salt, hashed) if salt and hashed else False
    if not (user_ok and pass_ok):
        return RedirectResponse('/login?err=1', status_code=303)
    sid = session_auth.mint_session()
    resp = RedirectResponse('/', status_code=303)
    resp.headers['Set-Cookie'] = session_auth.cookie_header(sid, secure=_is_https(request))
    return resp


@app.post('/logout')
async def logout(request: FastAPIRequest):
    session_auth.drop_session(request.headers.get('cookie'))
    resp = RedirectResponse('/login', status_code=303)
    resp.headers['Set-Cookie'] = session_auth.clear_cookie_header(_is_https(request))
    return resp

@app.get('/favicon.svg')
async def favicon():
    return Response(Path('favicon.svg').read_bytes(), media_type='image/svg+xml')

@app.get('/manifest.json')
async def manifest():
    return Response(Path('manifest.json').read_bytes(), media_type='application/manifest+json')

@app.get('/service-worker.js')
async def service_worker():
    # Service-Worker-Allowed lets the worker scope the whole site.
    # Cache-Control: no-cache so browsers pick up SW updates on every load.
    return Response(
        Path('service-worker.js').read_bytes(),
        media_type='application/javascript',
        headers={'Service-Worker-Allowed': '/', 'Cache-Control': 'no-cache'},
    )

@app.get('/icon-192.png')
async def icon_192():
    return Response(Path('icon-192.png').read_bytes(), media_type='image/png')

@app.get('/icon-512.png')
async def icon_512():
    return Response(Path('icon-512.png').read_bytes(), media_type='image/png')

@app.get('/apple-touch-icon.png')
async def apple_touch_icon():
    return Response(Path('apple-touch-icon.png').read_bytes(), media_type='image/png')

@app.get('/icon-maskable-512.png')
async def icon_maskable():
    return Response(Path('icon-maskable-512.png').read_bytes(), media_type='image/png')

VENDOR_DIR = Path(__file__).resolve().parent / 'vendor'

@app.get('/vendor/{filename}')
async def vendor_asset(filename: str):
    # Locally-hosted third-party JS/CSS (e.g. chart.js). Resolve and confine to
    # VENDOR_DIR so a crafted filename can't traverse outside it.
    candidate = (VENDOR_DIR / filename).resolve()
    try:
        candidate.relative_to(VENDOR_DIR)
    except ValueError:
        return Response(status_code=404)
    if not candidate.is_file():
        return Response(status_code=404)
    media = 'application/javascript' if candidate.suffix == '.js' else 'application/octet-stream'
    # 1-day cache + revalidate: vendor filenames aren't content-hashed, so
    # `immutable` would strand clients on an old copy if we ever bump a version.
    # The SRI integrity hash in index.html is the real guard against tampering.
    return Response(candidate.read_bytes(), media_type=media,
                    headers={'Cache-Control': 'public, max-age=86400, must-revalidate'})

@app.get('/api/state')
async def api_state():
    # Always return a dict with an explicit `connected` key so clients can
    # distinguish "never connected" (pre-first-reading) from "server returned
    # an empty body".
    last = dict(state['last']) if state['last'] else {}
    last['connected'] = bool(state.get('smoker_online'))
    last['probeUiTargets'] = state.get('probe_ui_targets', [None, None])[:]
    last['rssi'] = state.get('rssi')
    last['wifiRssi'] = state.get('wifiRssi')
    last['bleRssi'] = state.get('bleRssi')
    last['lastErr'] = state.get('lastErr') or ''
    last['connection'] = state.get('connection') or CONNECTION_LOCAL
    last['relay_host'] = state.get('relay_host') or DEFAULT_RELAY_HOST
    last['ip'] = state.get('ip')
    last['interval'] = state.get('interval')
    last['adapter'] = state.get('adapter')
    last['address'] = state.get('address')
    last['relay_name'] = state.get('relay_name') or ''
    last['relay_sta'] = state.get('relay_sta')
    return last

@app.get('/api/config')
async def get_config():
    return {
        'ntfy_topic': state.get('ntfy_topic') or '',
        'adapter': state.get('adapter') or '',
        'connection': state.get('connection') or CONNECTION_LOCAL,
        'relay_host': state.get('relay_host') or DEFAULT_RELAY_HOST,
        'login_user': state.get('login_user') or '',
        'login_configured': bool(state.get('login_hash')),
    }

@app.post('/api/config')
async def post_config(request: FastAPIRequest, body: dict):
    # Empty/missing ntfy_topic is ignored (preserves existing value). Prevents a
    # save that only meant to update adapter from silently wiping the topic and
    # leaving notify() as a no-op until the user notices alarms aren't firing.
    login_user = str(body.get('login_user', '')).strip()
    login_password = str(body.get('login_password', '') or body.get('password', '') or '')
    # Password present = credential write. Username-only saves (gear keep-blank) must not 400.
    if login_password:
        if session_auth.host_is_public(request.headers.get('host')):
            return JSONResponse({'ok': False, 'error': 'Login can only be changed on LAN'}, status_code=403)
        if not login_user or len(login_password) < 8:
            return JSONResponse({'ok': False, 'error': 'Username and 8+ character password required'}, status_code=400)
        save_login(login_user, login_password)
    submitted_topic = str(body.get('ntfy_topic', '')).strip()
    if submitted_topic:
        state['ntfy_topic'] = submitted_topic
    elif state.get('ntfy_topic'):
        print('Ignoring empty ntfy_topic in config save (preserving existing).')
    effective_topic = state.get('ntfy_topic') or ''

    adapter = str(body.get('adapter', '')).strip()
    old_adapter = state.get('adapter') or ''
    state['adapter'] = adapter or None
    adapter_changed = adapter != old_adapter

    submitted_conn = str(body.get('connection', '')).strip().lower()
    if submitted_conn and submitted_conn not in (CONNECTION_LOCAL, CONNECTION_RELAY):
        return JSONResponse({'ok': False, 'error': 'Invalid connection'}, status_code=400)
    old_conn = state.get('connection') or CONNECTION_LOCAL
    connection = submitted_conn or old_conn
    connection_changed = connection != old_conn
    state['connection'] = connection

    submitted_host = str(body.get('relay_host', '')).strip()
    if submitted_host:
        if not parse_relay_host(submitted_host):
            return JSONResponse({'ok': False, 'error': 'Relay host must be a LAN address'}, status_code=400)
        state['relay_host'] = submitted_host
    elif not state.get('relay_host'):
        state['relay_host'] = DEFAULT_RELAY_HOST

    if connection == CONNECTION_RELAY and not parse_relay_host(state.get('relay_host') or ''):
        return JSONResponse({'ok': False, 'error': 'Relay host must be a LAN address'}, status_code=400)

    save_config(effective_topic, adapter, state.get('probe_ui_targets'),
                connection=connection, relay_host=state.get('relay_host') or DEFAULT_RELAY_HOST)
    parts = [f'ntfy: {effective_topic or "(none)"}']
    if adapter_changed:
        parts.append(f'adapter: {adapter or "(auto)"}')
        print(f'Bluetooth adapter changed to {adapter or "(auto)"} — takes effect next scan cycle.')
    if connection_changed:
        label = 'ESP-32 relay' if connection == CONNECTION_RELAY else 'this server'
        parts.append(f'connection: {label}')
        print(f'Connection changed to {label} — takes effect next poll.')
    print(f'Config saved — {", ".join(parts)}')
    return {
        'ok': True,
        'ntfy_topic': effective_topic,
        'adapter': adapter,
        'adapter_changed': adapter_changed,
        'connection': connection,
        'connection_changed': connection_changed,
        'relay_host': state.get('relay_host') or DEFAULT_RELAY_HOST,
    }

@app.post('/api/probe-targets')
async def post_probe_targets(body: dict):
    raw = body.get('targets', [None, None])
    for i in range(2):
        v = raw[i] if i < len(raw) else None
        if v is None:
            state['probe_ui_targets'][i] = None
        else:
            try:
                iv = int(v)
                if 32 <= iv <= 500:
                    state['probe_ui_targets'][i] = iv
            except (ValueError, TypeError):
                pass
    save_config(state.get('ntfy_topic') or '', state.get('adapter') or '', state['probe_ui_targets'])
    return {'ok': True, 'targets': state['probe_ui_targets']}

def _adapter_footer_label() -> str:
    """hciN (real name) for the log footer. Never a fake id."""
    aid = state.get('adapter') or ''
    if not aid:
        return 'default'
    name = _adapter_names.get(aid) or ''
    return f'{aid} ({name})' if name else aid


def _list_adapters() -> list[dict]:
    """List available Bluetooth adapters with USB product names from sysfs."""
    adapters = []
    try:
        bt_dir = Path('/sys/class/bluetooth')
        if bt_dir.exists():
            for hci_link in sorted(bt_dir.iterdir()):
                hci_id = hci_link.name
                real = hci_link.resolve()
                usb_dev = real.parent
                while usb_dev != usb_dev.parent and not (usb_dev / 'product').exists():
                    usb_dev = usb_dev.parent
                product = _read_sysfs(usb_dev / 'product')
                manufacturer = _read_sysfs(usb_dev / 'manufacturer')
                if manufacturer and product:
                    name = f'{manufacturer} {product}' if manufacturer not in product else product
                elif product:
                    name = product
                else:
                    name = ''
                _adapter_names[hci_id] = name
                up = _is_adapter_up(hci_link)
                adapters.append({'id': hci_id, 'name': name, 'up': up})
    except Exception:
        log.exception('Failed to enumerate adapters')
    return adapters


@app.get('/api/adapters')
async def get_adapters():
    return {'adapters': _list_adapters(), 'current': state.get('adapter') or ''}


@app.get('/api/relays')
async def get_relays():
    """LAN discovery for Settings relay dropdown. Pick name+IP; no Bluetooth fields."""
    extras = []
    cur = state.get('relay_host') or ''
    if cur:
        extras.append(cur)
    extras.append(DEFAULT_RELAY_HOST)
    relays = await asyncio.to_thread(discover_lan_relays, extras)
    return {
        'relays': relays,
        'current': state.get('relay_host') or DEFAULT_RELAY_HOST,
    }


def _read_sysfs(path: Path) -> str:
    """Read a single-line sysfs attribute, returning '' on any failure."""
    try:
        return path.read_text().strip()
    except Exception:
        return ''

def _is_adapter_up(hci_link: Path) -> bool:
    """Return True if the HCI adapter is powered and running."""
    # /sys/class/bluetooth/hciX/flags is a hex bitfield; bit 0 == up, bit 4 == running.
    flags_raw = _read_sysfs(hci_link / 'flags')
    if flags_raw:
        try:
            flags = int(flags_raw, 16)
            return bool(flags & 0x1) and bool(flags & 0x10)
        except ValueError:
            pass
    return False

@app.post('/api/ntfy-test')
async def ntfy_test():
    topic = state.get('ntfy_topic')
    if not topic:
        return JSONResponse({'error': 'No ntfy topic configured'}, status_code=400)
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            None, _ntfy_post, topic,
            'BT Smoker Monitor Test',
            f'Test notification from BT Smoker Monitor.',
            'default', 'fire,white_check_mark'
        )
        return {'ok': True}
    except Exception as e:
        log.exception('ntfy test notification failed')
        return JSONResponse({'error': str(e)}, status_code=502)

@app.post('/api/clear-history')
async def clear_history():
    state['history'].clear()
    state['log_history'].clear()
    # Drop per-probe ETA state too so post-clear ETAs aren't computed off
    # pre-clear readings.
    state['probe_history'] = [[], []]
    state['probe_eta']     = [None, None]
    state['probe_stalled'] = [False, False]
    state['notified'] = {
        'probe_at_temp':      [False, False],
        'probe_over_temp':    [False, False],
        'grill_at_temp':      False,
        'grill_over_temp':    False,
        'grill_under_temp':   False,
        'grill_reached_once': False,
    }
    await broadcast({'type': 'clear_history'})
    return {'ok': True}

@app.websocket('/ws')
async def websocket_endpoint(ws: WebSocket):
    if session_auth.host_is_public(ws.headers.get('host')) and not session_auth.session_ok(ws.headers.get('cookie')):
        await ws.close(code=4401)
        return
    await ws.accept()
    clients[ws] = time.time()
    log.info(f'Client connected  ({len(clients)} total)')
    if state['history'] or state['log_history']:
        await ws.send_text(json.dumps({'type': 'history', 'data': state['history'], 'logs': state['log_history'], 'smoker_online': state['smoker_online'], 'probe_ui_targets': state['probe_ui_targets']}))
        # The initial history payload can be hundreds of KB over 24h; refresh
        # last-seen after a successful send so a slow mobile client isn't
        # reaped by the idle sweeper before it gets a chance to ack.
        clients[ws] = time.time()
    elif state['last'] and state['smoker_online']:
        await ws.send_text(json.dumps(state['last']))
    try:
        while True:
            # Any inbound frame refreshes the last-seen timestamp. Use receive()
            # rather than receive_text() so binary frames (rare but legal per
            # spec — some PWA bridges and proxies emit them) don't crash us.
            msg = await ws.receive()
            if msg.get('type') == 'websocket.disconnect':
                break
            clients[ws] = time.time()
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception('WebSocket loop error')
    finally:
        clients.pop(ws, None)
        log.info(f'Client disconnected  ({len(clients)} total)')

async def ws_reaper():
    """Drop WebSocket connections that have had no inbound activity for WS_IDLE_TIMEOUT_SECS."""
    while True:
        await asyncio.sleep(WS_REAPER_INTERVAL)
        now = time.time()
        stale = [ws for ws, ts in clients.items() if now - ts > WS_IDLE_TIMEOUT_SECS]
        for ws in stale:
            try:
                await ws.close(code=1001)
            except Exception:
                pass
            clients.pop(ws, None)
            log.info(f'Reaped idle WebSocket client  ({len(clients)} total)')

# ── Entry point ───────────────────────────────────────────────────────────────
async def main(interval: int, port: int, address: str | None, adapter: str | None, ntfy_topic: str):
    state['interval']   = interval
    state['ntfy_topic'] = ntfy_topic or None   # baseline from CLI/env

    # Config file persists settings across restarts
    cfg = load_config()
    if 'ntfy_topic' in cfg:
        state['ntfy_topic'] = cfg['ntfy_topic'] or None
    state['login_user'] = cfg.get('login_user') or ''
    state['login_salt'] = cfg.get('login_salt') or ''
    state['login_hash'] = cfg.get('login_hash') or ''

    # CLI --adapter wins, then config file, then None (system default)
    if adapter:
        state['adapter'] = adapter
    elif 'adapter' in cfg and cfg['adapter']:
        state['adapter'] = cfg['adapter']

    # Connection defaults to this server / local radio so a missing key
    # cannot flip a live cook onto the ESP-32 path.
    conn = cfg.get('connection') or CONNECTION_LOCAL
    state['connection'] = conn if conn in (CONNECTION_LOCAL, CONNECTION_RELAY) else CONNECTION_LOCAL
    host = cfg.get('relay_host') or DEFAULT_RELAY_HOST
    state['relay_host'] = host if parse_relay_host(host) else DEFAULT_RELAY_HOST

    # Restore UI-set probe targets from config
    if 'probe_targets' in cfg and isinstance(cfg['probe_targets'], list):
        for i in range(min(2, len(cfg['probe_targets']))):
            v = cfg['probe_targets'][i]
            if v is not None:
                try:
                    iv = int(v)
                    if 32 <= iv <= 500:
                        state['probe_ui_targets'][i] = iv
                except (ValueError, TypeError):
                    pass

    _list_adapters()
    print(f'ntfy topic : {state["ntfy_topic"] or "(disabled)"}')
    print(f'BT adapter : {state["adapter"] or "(system default)"}')
    if state['connection'] == CONNECTION_RELAY:
        print(f'Connection : ESP-32 relay ({state["relay_host"]})')
    else:
        print('Connection : this server (default)')
    print(f'Probe targets: {state["probe_ui_targets"]}')
    if address:
        print('Using hardcoded BLE address (discovery skipped).')
        state['address'] = address

    if AUTH_TOKEN:
        print('Auth enabled: /api/* routes require X-Auth-Token header.')
    else:
        print('WARNING: AUTH_TOKEN not set — /api/* is open. Set AUTH_TOKEN in env to lock it down.')

    print(f'Starting web server on http://0.0.0.0:{port}')

    server_cfg = uvicorn.Config(session_auth.PublicWsGate(app), host='0.0.0.0', port=port, log_level='warning')
    server     = uvicorn.Server(server_cfg)

    await asyncio.gather(
        poll_loop(interval),
        ws_reaper(),
        server.serve(),
    )

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='BT Smoker Monitor')
    parser.add_argument('--interval',   type=int, default=30,   help='Poll interval in seconds (default: 30)')
    parser.add_argument('--port',       type=int, default=8080, help='Web server port (default: 8080)')
    parser.add_argument('--address',    type=str, default=None, help='Hardcode BLE address, skipping discovery (e.g. AA:BB:CC:DD:EE:FF)')
    parser.add_argument('--adapter',    type=str, default=os.environ.get('BT_ADAPTER') or None, help='Bluetooth adapter to use (e.g. hci1). Defaults to BT_ADAPTER env var, then system default.')
    parser.add_argument('--ntfy-topic', type=str, default=os.environ.get('NTFY_TOPIC', ''), help='ntfy.sh topic for push notifications (or set NTFY_TOPIC env var)')
    parser.add_argument('--debug',      action='store_true', help='Enable verbose logging')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.debug else logging.WARNING,
        format='%(asctime)s  %(levelname)-8s  %(message)s',
        datefmt='%H:%M:%S',
    )
    asyncio.run(main(args.interval, args.port, args.address, args.adapter, args.ntfy_topic))
