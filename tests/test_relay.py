#!/usr/bin/env python3
"""Packet decode + LAN-only relay-host tests. No BLE hardware required."""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / 'server.py'
START = '# ── Pure helpers (unit-tested) ──────────────────────────────────────'
END = '# ── End pure helpers ──────────────────────────────────────────────────'


def load_helpers():
    text = SERVER.read_text(encoding='utf-8')
    i = text.find(START)
    j = text.find(END)
    if i < 0 or j < 0 or j <= i:
        raise SystemExit('Could not find pure-helper block in server.py')
    ns = {
        'ipaddress': ipaddress,
        're': re,
        'urlparse': urlparse,
        'json': json,
        'os': os,
        'logging': logging,
        'Path': Path,
    }
    src = 'from __future__ import annotations\n' + text[i:j]
    exec(compile(src, 'server.py', 'exec'), ns, ns)
    return ns


H = load_helpers()
# Don't let a developer config file or env var change the default these tests expect.
os.environ.pop('ALLOWED_NETS', None)
H['CONFIG_PATH'] = Path('/no/such/smoker-config.json')

# RFC 5737 documentation ranges. Not RFC1918, so they are allowed only when configured.
DOC_NETS = '192.0.2.0/24,198.51.100.0/24,203.0.113.0/24'
_MISSING_CONFIG = Path('/no/such/smoker-config.json')


@contextmanager
def set_allowed_nets(value: str | None):
    """Set ALLOWED_NETS for one assertion and drop the parse cache."""
    old = os.environ.get('ALLOWED_NETS')
    if value is None:
        os.environ.pop('ALLOWED_NETS', None)
    else:
        os.environ['ALLOWED_NETS'] = value
    H['_allowed_net_cache']['key'] = None
    try:
        yield
    finally:
        if old is None:
            os.environ.pop('ALLOWED_NETS', None)
        else:
            os.environ['ALLOWED_NETS'] = old
        H['_allowed_net_cache']['key'] = None


@contextmanager
def default_allowed_nets():
    """Unset env and point config at a missing file so the RFC1918 default applies."""
    had = 'CONFIG_PATH' in H
    old = H.get('CONFIG_PATH')
    H['CONFIG_PATH'] = _MISSING_CONFIG
    try:
        with set_allowed_nets(None):
            yield
    finally:
        if had:
            H['CONFIG_PATH'] = old
        else:
            H.pop('CONFIG_PATH', None)
        H['_allowed_net_cache']['key'] = None


@contextmanager
def config_allowed_nets(val):
    """Write allowed_nets into a temp config file and clear ALLOWED_NETS."""
    had = 'CONFIG_PATH' in H
    old = H.get('CONFIG_PATH')
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'config.json'
        path.write_text(json.dumps({'allowed_nets': val}), encoding='utf-8')
        H['CONFIG_PATH'] = path
        try:
            with set_allowed_nets(None):
                yield
        finally:
            if had:
                H['CONFIG_PATH'] = old
            else:
                H.pop('CONFIG_PATH', None)
            H['_allowed_net_cache']['key'] = None


def make_pkt(set_point, grill, t0, t1, p0, p1, extra=0):
    buf = bytearray(20 + extra)

    def u16(off, v):
        buf[off] = v & 0xFF
        buf[off + 1] = (v >> 8) & 0xFF

    u16(4, set_point)
    u16(6, grill)
    u16(8, t0)
    u16(10, t1)
    u16(16, p0)
    u16(18, p1)
    return bytes(buf)


class DecodePacketTests(unittest.TestCase):
    def test_offsets_match_nxe_spec(self):
        pkt = make_pkt(225, 227, 165, 200, 142, 199)
        dec = H['decode_packet'](pkt)
        self.assertEqual(dec, {
            'setPoint': 225,
            'grill': 227,
            'probeTargets': [165, 200],
            'probes': [142, 199],
        })

    def test_too_short_is_none(self):
        self.assertIsNone(H['decode_packet'](b'\x00' * 19))

    def test_le_u16_high_byte(self):
        pkt = make_pkt(256, 512, 1000, 0, 65535, 1)
        dec = H['decode_packet'](pkt)
        self.assertEqual(dec['setPoint'], 256)
        self.assertEqual(dec['grill'], 512)
        self.assertEqual(dec['probeTargets'], [1000, 0])
        self.assertEqual(dec['probes'], [65535, 1])


class RelayPayloadTests(unittest.TestCase):
    def test_valid_payload_strips_address(self):
        payload = {
            'ok': True,
            'setPoint': 225,
            'grill': 230,
            'probeTargets': [165, 200],
            'probes': [140, 190],
            'rssi': -61,
            'address': 'SHOULD-BE-IGNORED',
        }
        for fn in ('parse_relay_payload', 'reading_from_relay_payload'):
            dec = H[fn](payload)
            self.assertEqual(dec['setPoint'], 225)
            self.assertEqual(dec['grill'], 230)
            self.assertEqual(dec['probeTargets'], [165, 200])
            self.assertEqual(dec['probes'], [140, 190])
            self.assertNotIn('address', dec)

    def test_ok_false(self):
        self.assertIsNone(H['parse_relay_payload']({'ok': False, 'setPoint': 1}))

    def test_missing_fields(self):
        self.assertIsNone(H['parse_relay_payload']({'ok': True, 'grill': 1}))

    def test_short_lists(self):
        self.assertIsNone(H['parse_relay_payload']({
            'ok': True, 'setPoint': 1, 'grill': 1,
            'probeTargets': [1], 'probes': [1, 2],
        }))


class RelayHostTests(unittest.TestCase):
    def test_default_and_lan_ips(self):
        n = H['normalize_relay_host']
        self.assertEqual(n(''), H['DEFAULT_RELAY_HOST'])
        self.assertEqual(n('  192.168.4.1  '), '192.168.4.1')
        self.assertEqual(n('http://10.0.0.8/api/reading'), '10.0.0.8')
        self.assertEqual(n('10.0.0.8:80'), '10.0.0.8')
        self.assertEqual(n('10.0.0.8:8080'), '10.0.0.8:8080')

    def test_rejects_public_ip(self):
        self.assertIsNone(H['normalize_relay_host']('8.8.8.8'))
        self.assertIsNone(H['normalize_relay_host']('1.1.1.1:80'))
        self.assertIsNone(H['parse_relay_host']('example.com'))
        self.assertIsNone(H['parse_relay_host']('https://192.168.4.1'))
        self.assertIsNone(H['parse_relay_host']('0.0.0.0'))
        self.assertIsNone(H['parse_relay_host']('google.com'))
        self.assertIsNone(H['parse_relay_host']('192.168.4.1:99999'))

    def test_rejects_junk(self):
        self.assertIsNone(H['normalize_relay_host']('http://evil.example/x'))
        self.assertIsNone(H['normalize_relay_host']('host;rm'))
        self.assertIsNone(H['normalize_relay_host']('169.254.1.1\nX'))

    def test_allows_loopback_link_local_mdns(self):
        self.assertEqual(H['normalize_relay_host']('127.0.0.1'), '127.0.0.1')
        self.assertEqual(H['normalize_relay_host']('169.254.10.2'), '169.254.10.2')
        self.assertIsNotNone(H['parse_relay_host']('smoker-relay.local'))
        self.assertIsNotNone(H['parse_relay_host']('smoker-relay'))

    def test_lan_check_on_ips(self):
        with default_allowed_nets():
            self.assertTrue(H['relay_host_is_allowed']('192.168.4.1'))
            self.assertFalse(H['relay_host_is_allowed']('8.8.8.8'))
            self.assertFalse(H['relay_host_is_allowed'](''))
        with set_allowed_nets('192.0.2.0/24'):
            self.assertTrue(H['relay_host_is_allowed']('192.0.2.15:80'))
            self.assertFalse(H['relay_host_is_allowed']('198.51.100.15'))

    def test_default_allows_each_rfc1918_range_and_rejects_public(self):
        with default_allowed_nets():
            nets = [n for n in H['allowed_networks']() if n.version == 4]
            blocks = {(n.network_address.packed[0], n.network_address.packed[1]): n.prefixlen for n in nets}
            self.assertEqual(blocks[(10, 0)], 8)
            self.assertEqual(blocks[(172, 16)], 12)
            self.assertEqual(blocks[(192, 168)], 16)
            self.assertTrue(H['relay_host_is_allowed']('10.0.0.8'))
            self.assertTrue(H['relay_host_is_allowed']('10.10.10.1'))
            self.assertTrue(H['relay_host_is_allowed']('172.16.3.9'))
            self.assertTrue(H['relay_host_is_allowed']('172.18.0.4'))
            self.assertTrue(H['relay_host_is_allowed']('192.168.4.1'))
            self.assertTrue(H['relay_host_is_allowed']('192.168.77.4'))
            self.assertTrue(H['lan_scan_source_ok']('10.0.0.8'))
            self.assertTrue(H['lan_scan_source_ok']('172.16.3.9'))
            self.assertTrue(H['lan_scan_source_ok']('172.18.0.4'))
            self.assertTrue(H['lan_scan_source_ok']('192.168.77.4'))
            self.assertTrue(H['relay_host_is_allowed']('127.0.0.1'))
            self.assertFalse(H['lan_scan_source_ok']('127.0.0.1'))
            self.assertFalse(H['relay_host_is_allowed']('8.8.8.8'))
            self.assertFalse(H['relay_host_is_allowed']('203.0.113.10'))
            self.assertFalse(H['lan_scan_source_ok']('203.0.113.10'))
            self.assertFalse(H['lan_scan_source_ok']('192.0.2.23'))

    def test_custom_config_restricts_to_cidr(self):
        with set_allowed_nets('198.51.100.0/24'):
            self.assertTrue(H['relay_host_is_allowed']('198.51.100.20'))
            self.assertTrue(H['lan_scan_source_ok']('198.51.100.20'))
            self.assertFalse(H['relay_host_is_allowed']('192.0.2.10'))
            self.assertFalse(H['relay_host_is_allowed']('10.0.0.8'))
            self.assertFalse(H['relay_host_is_allowed']('172.16.3.9'))
            self.assertFalse(H['relay_host_is_allowed']('8.8.8.8'))
            self.assertFalse(H['relay_host_is_allowed']('169.254.10.2'))
            self.assertFalse(H['lan_scan_source_ok']('10.0.0.8'))

    def test_config_file_restricts_to_cidr(self):
        with config_allowed_nets(['203.0.113.0/24']):
            self.assertTrue(H['relay_host_is_allowed']('203.0.113.5'))
            self.assertTrue(H['lan_scan_source_ok']('203.0.113.5'))
            self.assertFalse(H['relay_host_is_allowed']('10.10.10.7'))
            self.assertFalse(H['relay_host_is_allowed']('192.0.2.10'))

    def test_env_overrides_config_file(self):
        had = 'CONFIG_PATH' in H
        old = H.get('CONFIG_PATH')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps({'allowed_nets': '203.0.113.0/24'}), encoding='utf-8')
            H['CONFIG_PATH'] = path
            try:
                with set_allowed_nets('192.0.2.0/24'):
                    self.assertTrue(H['relay_host_is_allowed']('192.0.2.8'))
                    self.assertFalse(H['relay_host_is_allowed']('203.0.113.5'))
            finally:
                if had:
                    H['CONFIG_PATH'] = old
                else:
                    H.pop('CONFIG_PATH', None)
                H['_allowed_net_cache']['key'] = None

    def test_invalid_allowed_nets_entries_skipped(self):
        with set_allowed_nets('not-a-cidr, 198.51.100.0/24, 999.1.1.1/24'):
            with self.assertLogs('smoker', level='WARNING') as captured:
                self.assertTrue(H['relay_host_is_allowed']('198.51.100.9'))
            text = '\n'.join(captured.output)
            self.assertIn('not-a-cidr', text)
            self.assertIn('999.1.1.1/24', text)
            self.assertFalse(H['relay_host_is_allowed']('10.0.0.1'))
            self.assertFalse(H['relay_host_is_allowed']('203.0.113.9'))

    def test_all_invalid_allowed_nets_fall_back_to_default(self):
        with set_allowed_nets('nope, still-nope'):
            with self.assertLogs('smoker', level='WARNING') as captured:
                self.assertTrue(H['relay_host_is_allowed']('10.0.0.8'))
            text = '\n'.join(captured.output)
            self.assertIn('nope', text)
            self.assertIn('default private ranges', text)
            self.assertTrue(H['relay_host_is_allowed']('172.18.0.1'))
            self.assertTrue(H['relay_host_is_allowed']('192.168.4.1'))
            self.assertFalse(H['relay_host_is_allowed']('8.8.8.8'))
            self.assertFalse(H['relay_host_is_allowed']('192.0.2.1'))

    def test_reading_url(self):
        self.assertEqual(
            H['relay_reading_url']('192.168.4.1'),
            'http://192.168.4.1/api/reading',
        )
        self.assertEqual(
            H['relay_reading_url']('10.0.0.5:8080'),
            'http://10.0.0.5:8080/api/reading',
        )
        self.assertIsNone(H['relay_reading_url']('8.8.8.8'))


class RelayHealthTests(unittest.TestCase):
    def setUp(self):
        self._nets = set_allowed_nets(DOC_NETS)
        self._nets.__enter__()

    def tearDown(self):
        self._nets.__exit__(None, None, None)

    def test_health_url(self):
        self.assertEqual(H['relay_health_url']('203.0.113.118'), 'http://203.0.113.118/health')
        self.assertIsNone(H['relay_health_url']('8.8.8.8'))

    def test_parse_with_name(self):
        hit = H['parse_relay_health']({
            'ok': True,
            'name': 'relay-a',
            'ble': False,
            'haveReading': True,
            'ap': '192.168.4.1',
            'sta': '203.0.113.118',
        }, '203.0.113.118')
        self.assertEqual(hit, {'name': 'relay-a', 'host': '203.0.113.118'})

    def test_parse_without_name_still_appears(self):
        # Live board before SoftAP-name flash.
        hit = H['parse_relay_health']({
            'ok': True,
            'ble': False,
            'haveReading': False,
            'ap': '192.168.4.1',
            'sta': '203.0.113.118',
        }, '192.0.2.50')
        self.assertEqual(hit['host'], '203.0.113.118')
        self.assertEqual(hit['name'], 'smoker-relay')


    def test_sanitize_display_name(self):
        self.assertEqual(H['sanitize_relay_display_name']('relay-a'), 'relay-a')
        self.assertEqual(H['sanitize_relay_display_name']('  '), 'smoker-relay')
        self.assertEqual(H['sanitize_relay_display_name']('x<script>'), 'xscript')
        self.assertEqual(H['sanitize_relay_display_name']("a'b&c"), 'abc')
        self.assertEqual(H['sanitize_relay_display_name'](None), 'smoker-relay')

    def test_rejects_random_json(self):
        self.assertIsNone(H['parse_relay_health']({'ok': True, 'status': 'up'}, '192.0.2.1'))
        self.assertIsNone(H['parse_relay_health']({'ok': False, 'name': 'x'}, '192.0.2.1'))

    def test_probe_hosts_from_local_slash24(self):
        # House LAN fixtures use RFC5737 TEST-NET-1 (192.0.2.0/24).
        hosts = H['discovery_probe_hosts'](['192.0.2.10'], ['203.0.113.118', '8.8.8.8'])
        self.assertIn('203.0.113.118', hosts)
        self.assertIn('192.0.2.1', hosts)
        self.assertNotIn('8.8.8.8', hosts)
        self.assertTrue(all(h.startswith(('192.0.2.', '203.0.113.')) for h in hosts))

    def test_custom_nets_limit_discovery(self):
        # Documentation ranges stand in for a house LAN. Other ranges are not scanned.
        with set_allowed_nets('192.0.2.0/24,203.0.113.0/24'):
            hosts = H['discovery_probe_hosts'](
                ['172.17.0.1', '198.51.100.1', '192.0.2.23'],
                ['203.0.113.118'],
            )
            self.assertIn('203.0.113.118', hosts)
            self.assertIn('192.0.2.1', hosts)
            self.assertTrue(all(h.startswith(('192.0.2.', '203.0.113.')) for h in hosts))
            self.assertFalse(any(h.startswith('172.17.') for h in hosts))
            self.assertFalse(any(h.startswith('198.51.100.') for h in hosts))
            self.assertFalse(H['lan_scan_source_ok']('172.17.0.1'))
            self.assertFalse(H['lan_scan_source_ok']('198.51.100.1'))
            self.assertTrue(H['lan_scan_source_ok']('192.0.2.23'))


if __name__ == '__main__':
    unittest.main(verbosity=2)


class RelayTelemetryTests(unittest.TestCase):
    def test_health_maps_rssi_and_err(self):
        tel = H['parse_relay_telemetry']({
            'ok': True, 'haveReading': False, 'ap': '', 'sta': '203.0.113.118',
            'wifiRssi': -66, 'bleRssi': -70, 'lastErr': 'ascii skip (not NXE temps)',
        })
        self.assertEqual(tel['wifiRssi'], -66)
        self.assertEqual(tel['bleRssi'], -70)
        self.assertEqual(tel['lastErr'], 'ascii skip (not NXE temps)')

    def test_health_v145_ble_counters_are_tolerated(self):
        payload = {
            'ok': True, 'name': 'smoker-relay', 'haveReading': True, 'ap': '', 'sta': '203.0.113.118',
            'wifiRssi': -60, 'bleRssi': -72, 'lastErr': 'connect failed (rc=13)', 'fw': 'v1.4.5',
            'bleLastError': 13, 'bleConsecConnectFails': 1, 'bleConnectFails': 4,
            'bleQuickRetryOk': 3, 'bleRescans': 1,
        }
        tel = H['parse_relay_telemetry'](payload)
        self.assertEqual(tel['bleRssi'], -72)
        self.assertEqual(tel['lastErr'], 'connect failed (rc=13)')

    def test_health_rejects_junk_rssi_and_ok_false(self):
        self.assertIsNone(H['parse_relay_telemetry']({'ok': False, 'wifiRssi': -50}))
        self.assertIsNone(H['parse_relay_telemetry']({'ok': True, 'foo': 1}))
        tel = H['parse_relay_telemetry']({
            'ok': True, 'sta': '203.0.113.118', 'wifiRssi': 5, 'bleRssi': True,
        })
        self.assertIsNone(tel['wifiRssi'])
        self.assertIsNone(tel['bleRssi'])
        self.assertEqual(tel['lastErr'], '')


class LinkTransitionTests(unittest.TestCase):
    def test_startup_already_offline_is_silent(self):
        self.assertIsNone(H['link_transition']('unknown', False))
        self.assertIsNone(H['link_transition'](None, False))

    def test_startup_already_online_is_silent(self):
        self.assertIsNone(H['link_transition']('unknown', True))

    def test_up_to_down_notifies_once(self):
        self.assertEqual(H['link_transition']('up', False), 'disconnected')
        self.assertIsNone(H['link_transition']('down', False))

    def test_down_to_up_notifies_once(self):
        self.assertEqual(H['link_transition']('down', True), 'connected')
        self.assertIsNone(H['link_transition']('up', True))


class RelayDebounceTests(unittest.TestCase):
    def test_stale_reason_defaults(self):
        self.assertEqual(H['relay_stale_reason'](''), 'relay BLE miss')
        self.assertEqual(H['relay_stale_reason'](None), 'relay BLE miss')
        self.assertEqual(H['relay_stale_reason']('connect failed'), 'connect failed')

    def test_stay_stale_for_first_two_misses(self):
        stay = H['relay_miss_should_stay_stale']
        self.assertTrue(stay(1, True))
        self.assertTrue(stay(2, True))
        self.assertFalse(stay(3, True))
        self.assertFalse(stay(4, True))

    def test_no_debounce_without_last_good(self):
        stay = H['relay_miss_should_stay_stale']
        self.assertFalse(stay(1, False))
        self.assertFalse(stay(2, False))
        self.assertFalse(stay(3, False))

    def test_threshold_constant(self):
        self.assertEqual(H['RELAY_OFFLINE_AFTER_FAILS'], 3)

