"""Silence button stops repeating sirens; a re-fired alarm sounds again."""
import json, shutil, subprocess
from pathlib import Path
import pytest

HTML = (Path(__file__).resolve().parent.parent / 'index.html').read_text()

def _slice(a, b):
    i = HTML.index(a); return HTML[i:HTML.index(b, i)]

def test_button_markup():
    assert 'id="silenceBtn"' in HTML and 'onclick="silenceAlarms()"' in HTML

@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_silence_flow():
    js = """
const els = {};
function mk(){ const c=new Set(); return {textContent:'', classList:{add:x=>c.add(x),remove:x=>c.delete(x),toggle:(x,on)=>on?c.add(x):c.delete(x),has:x=>c.has(x)}}; }
const document = { getElementById: id => (els[id] ||= mk()) };
let sirens = 0; function playSiren(){ sirens++; }
const window = {};
const timers = new Set(); let tid = 1;
globalThis.setInterval = () => { const t = tid++; timers.add(t); return t; };
globalThis.clearInterval = t => timers.delete(t);
""" + _slice('// ── Alarms', '// AudioContext is created lazily') + """
const r = {};
startGrillOverTempAlarm(); grillOverTempFired = true; startOverTempAlarm(0); overTempFired[0] = true;
r.shownWhileActive = els.silenceBtn.classList.has('show');
r.timersBefore = timers.size;
silenceAlarms();
r.timersAfter = timers.size;
r.hiddenAfter = !els.silenceBtn.classList.has('show');
r.stillAlertVisual = els.cardGrill.classList.has('over-temp-grill');
clearGrillOverTempAlarm(); const s0 = sirens; startGrillOverTempAlarm();
r.refires = sirens === s0 + 1 && els.silenceBtn.classList.has('show');
console.log(JSON.stringify(r));
"""
    r = json.loads(subprocess.run(['node', '-e', js], capture_output=True, text=True, check=True).stdout)
    assert r == {'shownWhileActive': True, 'timersBefore': 2, 'timersAfter': 0,
                 'hiddenAfter': True, 'stillAlertVisual': True, 'refires': True}
