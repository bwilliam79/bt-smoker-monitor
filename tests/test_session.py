"""Public-host session helpers. LAN Host is never treated as public."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from session_auth import (
    cookie_header,
    hash_password,
    host_is_public,
    mint_session,
    parse_sid,
    request_host,
    session_ok,
    verify_password,
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
