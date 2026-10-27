"""Chart thinning must still draw a line early in a cook (live tail point)."""
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
    # 30 s ticks starting 4 min into a bucket, with the :05 tick missed.
    base = 1793138700  # multiple of 300
    ticks = [base + 240, base + 270, base + 300 + 30, base + 300 + 60, base + 600, base + 630]
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
    assert res[0] == [base + 240]
    assert res[1] == [base + 240, base + 270]           # 2 points -> a line
    assert res[2] == [base + 240, base + 330]           # missed :05 tick, first in bucket kept
    assert res[3] == [base + 240, base + 330, base + 360]
    assert res[4] == [base + 240, base + 330, base + 600]
    assert res[5] == [base + 240, base + 330, base + 600, base + 630]
