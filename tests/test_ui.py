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
        self.assertIn("setInfo('infoAdapterLabel', 'Relay')", HTML)

    def test_relay_discovery_dropdown(self):
        self.assertIn('id="relaySelect"', HTML)
        self.assertIn('scanRelays()', HTML)
        self.assertIn('/api/relays', HTML)
        self.assertIn('Pick a relay on the LAN', HTML)
        self.assertIn('Or type IP', HTML)
        self.assertIn('Secondary fallback when discovery finds nothing', HTML)
        # Happy path is pick; manual wrap starts hidden.
        self.assertIn('id="relayManualWrap"', HTML)
        self.assertIn("manual.style.display = 'none'", HTML)
        self.assertIn("manual.style.display = ''", HTML)


class Defaults(unittest.TestCase):
    def test_default_local(self):
        self.assertIn("CONNECTION_LOCAL = 'local'", SERVER)
        self.assertIn("'connection':    CONNECTION_LOCAL", SERVER)

    def test_relay_is_lan_http(self):
        self.assertIn("http://", SERVER)
        self.assertIn('/api/reading', SERVER)
        self.assertIn('def parse_relay_host', SERVER)

    def test_relay_discovery_api(self):
        self.assertIn("/api/relays", SERVER)
        self.assertIn('def discover_lan_relays', SERVER)
        self.assertIn('def parse_relay_health', SERVER)


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

    def test_health_includes_name(self):
        self.assertIn('handleHealth', FIRMWARE)
        self.assertIn('"name"', FIRMWARE)
        self.assertIn('relayName', FIRMWARE)
        self.assertIn('name=name', FIRMWARE)  # SoftAP form field

    def test_softap_form_has_relay_name(self):
        self.assertIn('<label>Name</label>', FIRMWARE)
        self.assertIn('prefs.putString("name"', FIRMWARE)
        self.assertIn('action=/setpass', FIRMWARE)
        self.assertIn('action=/unlock', FIRMWARE)
        self.assertIn('sessionOk', FIRMWARE)
        self.assertIn('redirectHome', FIRMWARE)
        self.assertIn('Re-enter new password', FIRMWARE)
        self.assertNotIn('<label>Again</label>', FIRMWARE)

    def test_sta_wifi_form_not_on_lan(self):
        self.assertIn('wifi form is SoftAP only', FIRMWARE)
        self.assertIn('HTTP_POST, handleSave', FIRMWARE)
        self.assertIn('action=/save', FIRMWARE)
        self.assertNotIn('OTA password', FIRMWARE)

    def test_health_and_reading_stay_unauth(self):
        health = FIRMWARE.split('static void handleHealth', 1)[1].split('static void', 1)[0]
        reading = FIRMWARE.split('static void handleReading', 1)[1].split('static void', 1)[0]
        self.assertNotIn('requireAuth', health)
        self.assertNotIn('requireAuth', reading)
        self.assertIn('wifiRssi', FIRMWARE)
        self.assertIn('bleRssi', FIRMWARE)
        self.assertIn('lastErr', FIRMWARE)

    def test_log_strip_grill_ip_not_relay_sta(self):
        self.assertIn("n/a (no grill IP yet)", HTML)
        self.assertIn("infoBtMacWrap", HTML)
        self.assertIn("grillIp", HTML)
        self.assertNotIn("n/a (relay; MAC not published)", HTML)
        self.assertIn("macWrap.style.display = 'none'", HTML)

    def test_gatt_dump_and_poll_join(self):
        self.assertIn("/api/gatt", FIRMWARE)
        self.assertIn("pollOnce", FIRMWARE)
        self.assertNotIn("subscribeTempNotify", FIRMWARE)
        self.assertIn("decode_packet", FIRMWARE)
        self.assertIn("len < 20", FIRMWARE)
        self.assertNotIn("55 AA", FIRMWARE)
        self.assertNotIn("55 aa", FIRMWARE)
        gatt = FIRMWARE.split("static void handleGatt", 1)[1].split("static void", 1)[0]
        self.assertNotIn("requireAuth", gatt)

    def test_ota_auth_no_arduino_udp(self):
        self.assertIn('Update.h', FIRMWARE)
        self.assertIn('OTA_MAX', FIRMWARE)
        self.assertIn('pauseBle', FIRMWARE)
        self.assertIn('X-Relay-Password', FIRMWARE)
        self.assertIn('no password set', FIRMWARE)
        self.assertIn('prefs.remove("tok")', FIRMWARE)
        self.assertNotIn('nvs token minted', FIRMWARE)
        self.assertNotIn('X-Relay-Token', FIRMWARE)
        self.assertIn('headerTokenOk', FIRMWARE)
        self.assertNotIn('ArduinoOTA', FIRMWARE)
        self.assertNotIn('3232', FIRMWARE)
        ota = FIRMWARE.split('static void handleOtaPost', 1)[1].split('static void', 1)[0]
        self.assertIn('otaAuthOk', ota)

    def test_cook_ui_telemetry_lives_in_log_strip(self):
        self.assertNotIn('id="chipBtWrap"', HTML)
        self.assertNotIn('id="chipWifiWrap"', HTML)
        self.assertNotIn('id="chipErrWrap"', HTML)
        self.assertIn('id="infoLastErr"', HTML)
        self.assertIn('function updateLogStrip', HTML)
        self.assertIn("fetch('/api/state')", HTML)
        self.assertIn('infoBtMacWrap', HTML)
        header = HTML.split('<div class="dash">', 1)[0]
        self.assertNotIn('BT Signal', header)
        self.assertNotIn('chipWifi', header)
        self.assertIn('parse_relay_telemetry', SERVER)
        self.assertIn("last['wifiRssi']", SERVER)
        self.assertIn("last['relay_sta']", SERVER)
        self.assertIn("last['interval']", SERVER)
        save = FIRMWARE.split('static void handleSave', 1)[1].split('static void', 1)[0]
        self.assertIn('sessionOk', save)
        self.assertIn('BT to smoker', FIRMWARE)
        self.assertIn('FW_VERSION', FIRMWARE)
        self.assertIn('v1.4.3', FIRMWARE)
        self.assertIn('haveReading = false', FIRMWARE)
        self.assertIn('Stale cache made cook UI', FIRMWARE)
        self.assertNotIn('Resume BLE', FIRMWARE)
        self.assertNotIn('BLE held', FIRMWARE)
        self.assertNotIn('bleHeld', FIRMWARE)
        self.assertIn('pollOnce', FIRMWARE)
        self.assertNotIn('subscribeAll', FIRMWARE)
        self.assertIn('Connected', FIRMWARE)
        self.assertIn('Waiting for packet', FIRMWARE)
        self.assertIn('haveReading', FIRMWARE.split('static void sendPage', 1)[1].split('static void handleRoot', 1)[0])
        page = FIRMWARE.split('static void sendPage', 1)[1].split('static void handleRoot', 1)[0]
        self.assertNotIn('192.168.', page)
        self.assertNotIn('smoker ', page.split('BT to smoker', 1)[0])
        self.assertNotIn('setPoint', FIRMWARE.split('static void sendPage', 1)[1].split('static void handleRoot', 1)[0])


class DisconnectNotify(unittest.TestCase):
    def test_edge_latch_not_initialized_online(self):
        self.assertIn("def link_transition", SERVER)
        self.assertIn("'link':          'unknown'", SERVER)
        self.assertIn('async def _apply_offline', SERVER)
        self.assertNotIn('smoker_was_offline = False', SERVER)
        self.assertIn('disconnect alerts armed after the next reconnect', SERVER)


if __name__ == '__main__':
    unittest.main()


class PublicLogin(unittest.TestCase):
    def test_settings_fields(self):
        self.assertIn('id="loginUser"', HTML)
        self.assertIn('id="loginPass"', HTML)
        self.assertIn('login_password', HTML)
        self.assertIn('smoker.tehkernel.com', HTML)

    def test_server_wall(self):
        self.assertIn("host_is_public", SERVER)
        self.assertIn("@app.get('/login')", SERVER)
        self.assertIn("@app.post('/login')", SERVER)
        self.assertIn('save_login', SERVER)
        self.assertIn("Login can only be changed on LAN", SERVER)
        self.assertIn('https://smoker.tehkernel.com', SERVER)
        self.assertIn("/icon-192.png", SERVER)
        self.assertIn("/manifest.json", SERVER)
        self.assertIn("/apple-touch-icon.png", SERVER)
