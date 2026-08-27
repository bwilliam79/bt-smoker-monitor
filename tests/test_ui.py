"""Unit tests for Settings CONNECTION UI, firmware hygiene, and defaults."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / 'index.html').read_text(encoding='utf-8')
SERVER = (ROOT / 'server.py').read_text(encoding='utf-8')
FIRMWARE = (ROOT / 'firmware' / 'esp32-relay' / 'src' / 'main.cpp').read_text(encoding='utf-8')


class SettingsModal(unittest.TestCase):
    def test_connection_row_is_first(self):
        modal = HTML.split('<h2>', 1)[1]
        self.assertLess(modal.find('CONNECTION'), modal.find('Bluetooth Adapter'))
        self.assertLess(modal.find('Bluetooth Adapter'), modal.find('ntfy'))

    def test_labels_exact(self):
        self.assertIn('>CONNECTION<', HTML)
        self.assertIn('>This server</button>', HTML)
        self.assertIn('>ESP-32 relay</button>', HTML)

    def test_hides_and_disables_adapter_on_relay(self):
        self.assertIn('id="adapterRow"', HTML)
        self.assertIn("adapterSelect').disabled = relay", HTML)
        self.assertIn("The smoker talks to the ESP-32.", HTML)
        self.assertIn("adapterRow').style.display = relay ? 'none' : ''", HTML)

    def test_this_server_keeps_next_scan_hint(self):
        self.assertIn('Change takes effect on the next scan cycle.', HTML)

    def test_no_forced_first_open_pick(self):
        self.assertIn("connectionChoice = 'local'", HTML)
        self.assertIn("setConnectionChoice('local')", HTML)

    def test_forbidden_words_in_hints(self):
        self.assertNotIn('BLE stack', HTML)
        hint_bits = re.findall(r'class="modal-hint"[^>]*>(.*?)</span>', HTML, re.S)
        joined = ' '.join(hint_bits)
        self.assertNotIn('HCI', joined)
        self.assertNotIn('hci', joined.lower())
        self.assertNotIn('D-Bus', joined)

    def test_footer_relay_not_fake_hci(self):
        self.assertIn('infoAdapterLabel', HTML)
        self.assertIn("textContent = 'Relay'", HTML)


class Defaults(unittest.TestCase):
    def test_default_local(self):
        self.assertIn("CONNECTION_LOCAL = 'local'", SERVER)
        self.assertIn("'connection':    CONNECTION_LOCAL", SERVER)

    def test_relay_is_lan_http(self):
        self.assertIn("http://", SERVER)
        self.assertIn('/api/reading', SERVER)
        self.assertIn('def parse_relay_host', SERVER)


class Firmware(unittest.TestCase):
    def test_json_has_no_address_field(self):
        self.assertIn('setPoint', FIRMWARE)
        self.assertNotIn('"address"', FIRMWARE)

    def test_serial_does_not_print_address(self):
        for line in FIRMWARE.splitlines():
            if 'Serial.' in line:
                lower = line.lower()
                self.assertNotIn('address', lower, line)
                self.assertNotIn('targetaddr', lower, line)

    def test_lan_only_bind(self):
        self.assertIn('WebServer server(', FIRMWARE)


if __name__ == '__main__':
    unittest.main()
