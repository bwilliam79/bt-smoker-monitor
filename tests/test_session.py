"""Public-host session helpers. LAN Host is never treated as public."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from session_auth import (
    cookie_header,
    hash_password,
    host_is_public,
    mint_session,
    parse_sid,
    public_host,
    request_host,
    session_ok,
    verify_password,
    warn_if_public_login_off,
)


class HostGate(unittest.TestCase):
    def test_public_host(self):
        self.assertTrue(host_is_public('smoker.example.com'))
        self.assertTrue(host_is_public('Smoker.example.com:443'))

    def test_lan_is_open(self):
        self.assertFalse(host_is_public('203.0.113.23:8888'))
        self.assertFalse(host_is_public('127.0.0.1:8888'))
        self.assertFalse(host_is_public('localhost'))
        self.assertFalse(host_is_public('plate.example.com'))

    def test_strips_port(self):
        self.assertEqual(request_host('smoker.example.com:443'), 'smoker.example.com')

    def test_env_unset_or_empty_falls_back_to_placeholder(self):
        env = {k: v for k, v in os.environ.items() if k != 'PUBLIC_HOST'}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(public_host(), 'smoker.example.com')
            self.assertTrue(host_is_public('smoker.example.com'))
            self.assertTrue(host_is_public('SMOKER.EXAMPLE.COM:443'))
            self.assertFalse(host_is_public('plate.example.com'))
        for blank in ('', '   '):
            with patch.dict(os.environ, {'PUBLIC_HOST': blank}):
                self.assertEqual(public_host(), 'smoker.example.com')

    def test_env_host_requires_login_and_other_hosts_stay_open(self):
        with patch.dict(os.environ, {'PUBLIC_HOST': 'pit.example.test'}):
            self.assertEqual(public_host(), 'pit.example.test')
            self.assertTrue(host_is_public('pit.example.test'))
            self.assertTrue(host_is_public('PIT.EXAMPLE.TEST:443'))
            self.assertFalse(host_is_public('smoker.example.com'))
            self.assertFalse(host_is_public('203.0.113.23:8888'))
            self.assertFalse(host_is_public('localhost'))
            self.assertFalse(host_is_public('plate.example.com'))

    def test_env_value_matches_case_insensitively_with_port(self):
        # Both sides are lowercased and the port is stripped.
        with patch.dict(os.environ, {'PUBLIC_HOST': 'SMOKER.EXAMPLE.COM:443'}):
            self.assertEqual(public_host(), 'smoker.example.com')
            self.assertTrue(host_is_public('smoker.example.com'))
            self.assertTrue(host_is_public('SMOKER.EXAMPLE.COM:443'))
            self.assertTrue(host_is_public('Smoker.example.com:80'))

    def test_env_url_keeps_the_host(self):
        with patch.dict(os.environ, {'PUBLIC_HOST': 'https://pit.example.test'}):
            self.assertEqual(public_host(), 'pit.example.test')
            self.assertTrue(host_is_public('PIT.EXAMPLE.TEST:443'))
            self.assertFalse(host_is_public('https'))
        with patch.dict(os.environ, {'PUBLIC_HOST': 'https://pit.example.test/login'}):
            self.assertEqual(public_host(), 'pit.example.test')
        with patch.dict(os.environ, {'PUBLIC_HOST': 'https://SMOKER.EXAMPLE.COM:443'}):
            self.assertEqual(public_host(), 'smoker.example.com')
            self.assertTrue(host_is_public('smoker.example.com'))


class PublicHostWarning(unittest.TestCase):
    def test_warns_for_placeholder_not_for_other_host(self):
        with patch.dict(os.environ, {'PUBLIC_HOST': 'smoker.example.com'}):
            with self.assertLogs('session_auth', level='WARNING') as captured:
                warn_if_public_login_off()
        self.assertTrue(any('remote login is effectively off' in line for line in captured.output))

        env = {k: v for k, v in os.environ.items() if k != 'PUBLIC_HOST'}
        with patch.dict(os.environ, env, clear=True):
            with self.assertLogs('session_auth', level='WARNING') as captured:
                warn_if_public_login_off()
        self.assertTrue(any('remote login is effectively off' in line for line in captured.output))

        with patch.dict(os.environ, {'PUBLIC_HOST': 'smoker.test.invalid'}):
            with self.assertNoLogs('session_auth', level='WARNING'):
                warn_if_public_login_off()
        with patch.dict(os.environ, {'PUBLIC_HOST': 'https://smoker.test.invalid'}):
            with self.assertNoLogs('session_auth', level='WARNING'):
                warn_if_public_login_off()
        with patch.dict(os.environ, {'PUBLIC_HOST': 'https://smoker.example.com'}):
            with self.assertLogs('session_auth', level='WARNING') as captured:
                warn_if_public_login_off()
        self.assertTrue(any('remote login is effectively off' in line for line in captured.output))


class Password(unittest.TestCase):
    def test_roundtrip(self):
        salt, hashed = hash_password('kitchen8x')
        self.assertTrue(verify_password('kitchen8x', salt, hashed))
        self.assertFalse(verify_password('wrong', salt, hashed))


class SessionCookie(unittest.TestCase):
    def test_mint_and_parse(self):
        sid = mint_session()
        header = cookie_header(sid, secure=True)
        self.assertIn('HttpOnly', header)
        self.assertIn('SameSite=Lax', header)
        self.assertIn('Secure', header)
        self.assertTrue(session_ok(f'sid={sid}'))
        self.assertEqual(parse_sid(f'sid={sid}; other=1'), sid)
        self.assertFalse(session_ok('sid=nope'))


if __name__ == '__main__':
    unittest.main()
