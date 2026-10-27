"""Chart plots one point per minute, exactly on the minute mark."""
import json, re, shutil, subprocess
from pathlib import Path
import pytest

HTML = (Path(__file__).resolve().parent.parent / 'index.html').read_text()

def _extract():
    start = HTML.index('const CHART_POINT_MIN_GAP_MS')
    decl_end = HTML.index('function makeDataset', start)
    fn_start = HTML.index('function pushChartPoint')
    fn_end = HTML.index('// ── Dashboard update', fn_start)
    return HTML[start:decl_end] + HTML[fn_start:fn_end]

@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_fresh_cook_draws_line_and_buckets():
    # Ticks inside 1-minute buckets, with the on-the-minute tick missed once.
    base = 1793138700  # multiple of 60
    ticks = [base + 40, base + 50, base + 60 + 10, base + 60 + 20, base + 120, base + 125]
    js = f"""
const CHART_WINDOW_MS = 24*3600*1000, PROBE_DISCONNECTED = 999;
const chart = {{ update(){{}} }}; function syncChartEmpty(){{}}
function fmtTime(d){{ return String(d.getTime()); }}
function effectiveProbeTarget(){{ return null; }}
{_extract()}
const out = [];
for (const t of {json.dumps(ticks)}) {{
  pushChartPoint({{grill: 100, setPoint: 225, probes: [999, 999]}}, new Date(t*1000), true);
  out.push(chartTimestamps.map(x => x/1000));
}}
console.log(JSON.stringify(out));
"""
    res = json.loads(subprocess.run(['node', '-e', js], capture_output=True, text=True, check=True).stdout)
    assert res[0] == [base]                             # snapped to the minute
    assert res[1] == [base]                             # same minute: skipped
    assert res[2] == [base, base + 60]                  # missed :00 tick, still plotted on the minute
    assert res[3] == [base, base + 60]
    assert res[4] == [base, base + 60, base + 120]
    assert res[5] == [base, base + 60, base + 120]
