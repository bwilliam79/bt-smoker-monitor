"""Per-card Silence buttons stop that card's siren; locked audio plays first."""
import json, shutil, subprocess
from pathlib import Path
import pytest

HTML = (Path(__file__).resolve().parent.parent / 'index.html').read_text()

def _slice(a, b):
    i = HTML.index(a); return HTML[i:HTML.index(b, i)]

def test_button_markup_on_each_card():
    for card, btn, arg in [('cardGrill', 'silenceBtnGrill', "'grill'"), ('cardP1', 'silenceBtnP1', '0'), ('cardP2', 'silenceBtnP2', '1')]:
        start = HTML.index(f'id="{card}"'); end = HTML.index('</article>', start)
        assert f'id="{btn}"' in HTML[start:end] and f'onclick="silenceAlarms({arg})"' in HTML[start:end]

def _run(body, locked=False):
    js = """
const els = {};
function mk(){ const c=new Set(); return {textContent:'', classList:{add:x=>c.add(x),remove:x=>c.delete(x),toggle:(x,on)=>on?c.add(x):c.delete(x),has:x=>c.has(x)}}; }
const document = { getElementById: id => (els[id] ||= mk()) };
const window = {};
let audioCtx = %s; function unlockAudio(){ audioCtx = {}; }
let sirens = 0; function playSiren(){ sirens++; }
const timers = new Set(); let tid = 1;
globalThis.setInterval = () => { const t = tid++; timers.add(t); return t; };
globalThis.clearInterval = t => timers.delete(t);
const shown = id => els[id].classList.has('show');
""" % ('null' if locked else '{}') + _slice('// ── Alarms', '// AudioContext is created lazily') + body
    out = subprocess.run(['node', '-e', js], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-600:]
    return json.loads(out.stdout)

node = pytest.mark.skipif(not shutil.which('node'), reason='node not installed')

@node
def test_silence_only_that_card():
    r = _run("""
startGrillOverTempAlarm(); grillOverTempFired = true; startOverTempAlarm(0); overTempFired[0] = true;
const a = {grill: shown('silenceBtnGrill'), p1: shown('silenceBtnP1'), p2: shown('silenceBtnP2')};
silenceAlarms('grill');
const b = {grill: shown('silenceBtnGrill'), p1: shown('silenceBtnP1'), timers: timers.size,
           visual: els.cardGrill.classList.has('over-temp-grill')};
silenceAlarms(0);
clearGrillOverTempAlarm(); const s0 = sirens; startGrillOverTempAlarm();
console.log(JSON.stringify({a, b, after: timers.size, refires: sirens === s0 + 1 && shown('silenceBtnGrill')}));
""")
    assert r['a'] == {'grill': True, 'p1': True, 'p2': False}
    assert r['b'] == {'grill': False, 'p1': True, 'timers': 1, 'visual': True}
    assert r['after'] == 1 and r['refires'] is True

@node
def test_locked_audio_first_tap_plays_instead_of_silencing():
    r = _run("""
startGrillOverTempAlarm();
const label1 = els.silenceBtnGrill.textContent;
silenceAlarms('grill');
const r = {label1, label2: els.silenceBtnGrill.textContent, timers: timers.size, sirens};
silenceAlarms('grill'); r.timersAfter = timers.size;
console.log(JSON.stringify(r));
""", locked=True)
    assert r == {'label1': 'Tap to hear alarm', 'label2': 'Silence alarm', 'timers': 1, 'sirens': 2, 'timersAfter': 0}
