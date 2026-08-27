#!/usr/bin/env python3
"""Packet decode + LAN-only relay-host tests. No BLE hardware required."""
from __future__ import annotations

import ipaddress
import json
import re
import tempfile
import unittest
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
    }
    src = 'from __future__ import annotations\n' + text[i:j]
    exec(compile(src, 'server.py', 'exec'), ns, ns)
    return ns


H = load_helpers()


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
        self.assertTrue(H['relay_host_is_allowed']('192.168.4.1'))
        self.assertTrue(H['relay_host_is_allowed']('10.1.2.3:80'))
        self.assertFalse(H['relay_host_is_allowed']('8.8.8.8'))
        self.assertFalse(H['relay_host_is_allowed'](''))

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
    def test_health_url(self):
        self.assertEqual(H['relay_health_url']('192.168.1.118'), 'http://192.168.1.118/health')
        self.assertIsNone(H['relay_health_url']('8.8.8.8'))

    def test_parse_with_name(self):
        hit = H['parse_relay_health']({
            'ok': True,
            'name': 'patio',
            'ble': False,
            'haveReading': True,
            'ap': '192.168.4.1',
            'sta': '192.168.1.118',
        }, '192.168.1.118')
        self.assertEqual(hit, {'name': 'patio', 'host': '192.168.1.118'})

    def test_parse_without_name_still_appears(self):
        # Live board before SoftAP-name flash.
        hit = H['parse_relay_health']({
            'ok': True,
            'ble': False,
            'haveReading': False,
            'ap': '192.168.4.1',
            'sta': '192.168.1.118',
        }, '192.168.1.50')
        self.assertEqual(hit['host'], '192.168.1.118')
        self.assertEqual(hit['name'], 'smoker-relay')


    def test_sanitize_display_name(self):
        self.assertEqual(H['sanitize_relay_display_name']('patio'), 'patio')
        self.assertEqual(H['sanitize_relay_display_name']('  '), 'smoker-relay')
        self.assertEqual(H['sanitize_relay_display_name']('x<script>'), 'xscript')
        self.assertEqual(H['sanitize_relay_display_name']("a'b&c"), 'abc')
        self.assertEqual(H['sanitize_relay_display_name'](None), 'smoker-relay')

    def test_rejects_random_json(self):
        self.assertIsNone(H['parse_relay_health']({'ok': True, 'status': 'up'}, '192.168.1.1'))
        self.assertIsNone(H['parse_relay_health']({'ok': False, 'name': 'x'}, '192.168.1.1'))

    def test_probe_hosts_from_local_slash24(self):
        hosts = H['discovery_probe_hosts'](['192.168.1.10'], ['192.168.1.118', '8.8.8.8'])
        self.assertIn('192.168.1.118', hosts)
        self.assertIn('192.168.1.1', hosts)
        self.assertNotIn('8.8.8.8', hosts)
        self.assertTrue(all(h.startswith('192.168.1.') for h in hosts))



    def test_skips_docker_and_libvirt_bridges(self):
        hosts = H['discovery_probe_hosts'](
            ['192.168.1.23', '172.17.0.1', '192.168.122.1'],
            ['192.168.1.118'],
        )
        self.assertIn('192.168.1.118', hosts)
        self.assertTrue(all(h.startswith('192.168.1.') for h in hosts))
        self.assertNotIn('172.17.0.1', hosts)
        self.assertNotIn('192.168.122.1', hosts)

if __name__ == '__main__':
    unittest.main(verbosity=2)
