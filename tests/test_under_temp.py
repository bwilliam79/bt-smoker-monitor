"""Smoker under-temp alarm arms on overshoot and survives a reload."""
import json, shutil, subprocess
from pathlib import Path
import pytest

HTML = (Path(__file__).resolve().parent.parent / 'index.html').read_text()

def _slice(a, b):
    i = HTML.index(a); return HTML[i:HTML.index(b, i)]

PRELUDE = """
const els = {};
function mk(){ const c=new Set(); return {textContent:'', classList:{add:x=>c.add(x),remove:x=>c.delete(x),toggle:(x,on)=>on?c.add(x):c.delete(x),has:x=>c.has(x)}}; }
const document = { getElementById: id => (els[id] ||= mk()), addEventListener(){} };
const window = {};
let sirens = 0;
globalThis.setInterval = () => 1; globalThis.clearInterval = () => {};
function log(){}
const PROBE_DISCONNECTED = 999;
function effectiveProbeTarget(){ return null; }
"""

def _run(body):
    js = PRELUDE + _slice('// ── Alarms', '// ── WebSocket') + "\nplaySiren = () => { sirens++; };\n" + body
    out = subprocess.run(['node', '-e', js], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-600:]
    return json.loads(out.stdout)

@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_overshoot_then_drop_fires_under_temp():
    r = _run("""
for (const g of [180, 260, 240, 212]) checkAlarms({grill: g, setPoint: 225, probes: [999, 999]});
console.log(JSON.stringify({under: grillUnderTempFired, sirens}));
""")
    assert r['under'] is True and r['sirens'] >= 2   # over-temp siren + under-temp siren

@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_warming_up_does_not_fire_under_temp():
    r = _run("""
for (const g of [120, 160, 200]) checkAlarms({grill: g, setPoint: 225, probes: [999, 999]});
console.log(JSON.stringify({under: grillUnderTempFired}));
""")
    assert r['under'] is False

@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_reload_seed_uses_history_at_current_set_point():
    r = _run("""
const pts = (arr) => arr.map(([g, sp]) => ({grill: g, setPoint: sp}));
console.log(JSON.stringify([
  smokerReachedSetPoint(pts([[150,225],[230,225],[205,225]])),
  smokerReachedSetPoint(pts([[260,250],[200,225],[205,225]])),
  smokerReachedSetPoint([]),
]));
""")
    assert r == [True, False, False]
