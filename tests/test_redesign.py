"""Pit Instrument dashboard: read-only smoker target, signal tones, fit shell."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / 'index.html').read_text(encoding='utf-8')
SW = (ROOT / 'service-worker.js').read_text(encoding='utf-8')
VERSION = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()


def _signal_state(rssi):
    """Mirror of index.html signalState. Boundaries: -65 Good, -80 Fair."""
    if rssi >= -65:
        return ('Good', 'ok')
    if rssi >= -80:
        return ('Fair', 'warn')
    return ('Weak', 'alert')


class ReadonlySmokerTarget(unittest.TestCase):
    def test_smoker_tile_has_no_input(self):
        grill = HTML.split('id="cardGrill"', 1)[1].split('id="cardP1"', 1)[0]
        self.assertNotIn('<input', grill.lower())
        self.assertNotIn('type="number"', grill)
        self.assertIn('id="setGrill"', grill)
        self.assertIn('read-only', grill)
        self.assertIn('id="grillAtTempTimer"', grill)
        self.assertNotIn('ETA', grill)

    def test_no_setpoint_write_path(self):
        for bad in (
            '/api/setpoint', '/api/set-point', '/api/grill-target',
            'setSmokerTarget', 'writeSetPoint', "getElementById('setGrill').value",
        ):
            self.assertNotIn(bad, HTML)
        self.assertIn("getElementById('setGrill').textContent", HTML)
        # Probe alert thresholds stay editable. Smoker target is not a number field.
        self.assertEqual(len(re.findall(r'type="number"', HTML)), 2)
        self.assertIn('id="targetInputP1"', HTML)
        self.assertIn('id="targetInputP2"', HTML)
        self.assertIn('/api/probe-targets', HTML)

    def test_three_tiles_one_row_markup(self):
        self.assertIn('<div class="dash">', HTML)
        self.assertIn('>Smoker<', HTML)
        self.assertIn('>Probe 1<', HTML)
        self.assertIn('>Probe 2<', HTML)
        self.assertIn('repeat(3, minmax(0, 1fr))', HTML)


class AlarmLatch(unittest.TestCase):
    def test_reached_once_is_declared(self):
        # An undeclared read throws on the first live packet and skips alarms.
        self.assertRegex(HTML, r'let\s+grillReachedOnce\s*=\s*false')


class SignalThresholds(unittest.TestCase):
    def test_source_order(self):
        m = re.search(r'function signalState\(rssi\) \{([\s\S]*?\n)\}', HTML)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertLess(body.find('rssi >= -65'), body.find('rssi >= -80'))
        self.assertIn("label: 'Good'", body)
        self.assertIn("tone: 'ok'", body)
        self.assertIn("label: 'Fair'", body)
        self.assertIn("tone: 'warn'", body)
        self.assertIn("label: 'Weak'", body)
        self.assertIn("tone: 'alert'", body)
        self.assertIn(r'\u25CF', HTML)
        self.assertIn(r'\u2212', HTML)

    def test_boundaries(self):
        self.assertEqual(_signal_state(-65), ('Good', 'ok'))
        self.assertEqual(_signal_state(-64), ('Good', 'ok'))
        self.assertEqual(_signal_state(-66), ('Fair', 'warn'))
        self.assertEqual(_signal_state(-80), ('Fair', 'warn'))
        self.assertEqual(_signal_state(-81), ('Weak', 'alert'))
        self.assertEqual(_signal_state(-120), ('Weak', 'alert'))

    def test_palette_tokens(self):
        self.assertIn('--ok: #3DDC97', HTML)
        self.assertIn('--warn: #F5B841', HTML)
        self.assertIn('--alert: #FF5A52', HTML)


class Shell(unittest.TestCase):
    def test_empty_copy(self):
        self.assertIn('Live temperatures appear here once the smoker connects.', HTML)
        self.assertIn("The server can't reach the smoker. It keeps retrying on its own.", HTML)
        self.assertIn('No readings yet. The chart fills in once the smoker connects.', HTML)

    def test_version_only_in_settings(self):
        self.assertEqual(VERSION, '1.4.0')
        self.assertNotIn('version-footer', HTML)
        self.assertNotIn('<footer', HTML.lower())
        self.assertEqual(HTML.count('{{VERSION}}'), 1)
        self.assertIn('class="modal-version">v{{VERSION}}', HTML)
        self.assertLess(HTML.find('saveSettings()'), HTML.find('v{{VERSION}}'))
        self.assertIn("const CACHE_NAME = 'bt-smoker-v1.4.0'", SW)

    def test_fit_and_no_horizontal_scroll_hooks(self):
        self.assertIn('class="fit booting"', HTML)
        self.assertIn('100dvh', HTML)
        self.assertIn('overflow-x: hidden', HTML)
        self.assertIn('min-width: 560px', HTML)
        self.assertIn('class="log-scroll"', HTML)
        self.assertRegex(HTML, r'id="logBody"[^>]*style="display:none"')
        self.assertIn('minmax(0, 1fr)', HTML)

    def test_adapter_inside_log_body(self):
        body = HTML.split('id="logBody"', 1)[1].split('id="log"', 1)[0]
        self.assertIn('id="infoAdapterLabel"', body)
        self.assertIn('>Adapter<', body)
        self.assertIn('id="infoAdapter"', body)
        self.assertIn('id="infoWifi"', body)
        self.assertIn('id="infoLastErr"', body)
        self.assertIn('id="infoBtMacWrap"', body)

    def test_no_new_cdn_or_font(self):
        for bad in ('fonts.googleapis', 'fonts.gstatic', 'cdn.tailwindcss', 'iconify', 'unpkg.com', 'jsdelivr'):
            self.assertNotIn(bad, HTML)


if __name__ == '__main__':
    unittest.main()
