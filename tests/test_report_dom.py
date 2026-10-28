"""Report interaction checks in a real (headless) browser: the slider must survive input events.

Skipped when no Chrome/Chromium is installed. The check script is appended to a rendered report and writes its
verdict to <body data-selftest>, which --dump-dom returns.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from compactlab.adapters import get_adapter
from compactlab.pricing import PricingTable
from compactlab.report.html import render_html
from compactlab.sim.engine import analyze

CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome",
    "chromium",
    "chromium-browser",
]

CHECK = """
<script>
(function(){
  const fail=[];const ok=(c,m)=>{if(!c)fail.push(m)};
  try{
    const n0=document.getElementById('rng');const g=D.groups[S.gi];const max=g.grid.length-1;
    n0.focus();
    for(const v of [0,3,Math.min(7,max),2,max,1]){
      n0.value=v;n0.dispatchEvent(new Event('input',{bubbles:true}));
      ok(document.getElementById('rng')===n0,'slider node replaced after input '+v);
      ok(S.T===g.grid[v],'state not updated for '+v);
      ok(document.getElementById('tval').textContent===fk(g.grid[v]),'value label stale at '+v);
    }
    ok(document.activeElement===n0,'slider lost focus');
    ok(document.querySelector('[data-gear=custom]').getAttribute('aria-pressed')==='true','custom not active');
    document.querySelector('[data-gear=time]').click();
    ok(document.getElementById('rng')===n0,'slider node replaced after profile click');
    ok(+n0.value===g.grid.indexOf(g.profiles.time.threshold),'slider not moved to the time profile');
    ok(document.querySelector('[data-gear=time]').getAttribute('aria-pressed')==='true','time not active');
    const def=document.querySelector('.mk[data-ref=rDefault]');ok(def,'default mark missing');
    if(def){def.click();ok(S.gear==='custom'&&document.getElementById('rng')===n0,'default mark did not snap')}
    const pm=document.querySelector('.mk[data-g=balanced]');ok(pm,'balanced mark missing');
    if(pm){pm.click();ok(S.gear==='balanced','balanced mark did not select the profile')}
    const b=document.querySelector('#fCopy .seg[data-k=profile] button[data-v=time]');
    b.focus();b.click();
    ok(document.activeElement&&document.activeElement.dataset.v==='time','copy panel lost focus');
    // hover tooltip on the cost chart
    const hv=document.querySelector('#fCost rect.hov');ok(hv,'no hover layer on the cost chart');
    if(hv){const r=hv.getBoundingClientRect();
      const at={bubbles:true,clientX:r.left+r.width/2,clientY:r.top+r.height/2};
      hv.dispatchEvent(new PointerEvent('pointermove',at));
      const tip=document.getElementById('tip');ok(!tip.hidden&&/k/.test(tip.textContent),'tooltip not shown')}
    // the control bar stays visible while scrolling
    scrollTo(0,document.body.scrollHeight);
    ok(Math.abs(document.getElementById('bar').getBoundingClientRect().top)<1,'control bar is not sticky');
  }catch(e){fail.push('error: '+e.message)}
  document.body.dataset.selftest=fail.length?'fail: '+fail.join(' | '):'ok';
})();
</script>
"""


def _chrome() -> str | None:
    for c in CANDIDATES:
        p = shutil.which(c) or (c if Path(c).exists() else None)
        if p:
            return p
    return None


@pytest.mark.skipif(_chrome() is None, reason="no Chrome/Chromium for the DOM check")
def test_slider_survives_input_events(tmp_path, claude_root, codex_root):
    chrome = _chrome()
    assert chrome
    ss = list(get_adapter("claude-code")(claude_root).sessions()) + list(get_adapter("codex")(codex_root).sessions())
    res = analyze(ss, PricingTable.load(offline=True), current={("claude-code", "claude-opus-5-5"): (600_000, "t")})
    page = tmp_path / "r.html"
    page.write_text(render_html(res).replace("</body>", CHECK + "</body>"))
    out = subprocess.run(
        [chrome, "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={tmp_path / 'prof'}",
         "--dump-dom", page.as_uri()],
        capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert 'data-selftest="ok"' in out.stdout, out.stdout.split("data-selftest=")[-1][:400]


@pytest.mark.skipif(_chrome() is None, reason="no Chrome/Chromium for the DOM check")
@pytest.mark.parametrize("width", [375, 768, 1440])
def test_no_horizontal_scroll(tmp_path, claude_root, codex_root, width):
    chrome = _chrome()
    assert chrome
    ss = list(get_adapter("claude-code")(claude_root).sessions()) + list(get_adapter("codex")(codex_root).sessions())
    res = analyze(ss, PricingTable.load(offline=True), current={("claude-code", "claude-opus-5-5"): (600_000, "t")})
    page = tmp_path / "r.html"
    page.write_text(render_html(res))
    host = tmp_path / "host.html"
    host.write_text(
        f'<!doctype html><body><iframe id="f" src="{page.as_uri()}" style="width:{width}px;height:900px;border:0">'
        "</iframe><script>f.onload=()=>setTimeout(()=>{const d=f.contentDocument.documentElement;"
        "document.body.dataset.sw=d.scrollWidth+'/'+d.clientWidth},300)</script>"
    )
    out = subprocess.run(
        [chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--allow-file-access-from-files",
         f"--user-data-dir={tmp_path / 'prof'}", "--virtual-time-budget=3000", "--window-size=1500,1000",
         "--dump-dom", host.as_uri()],
        capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    sw, cw = out.stdout.split('data-sw="')[1].split('"')[0].split("/")
    assert int(sw) <= int(cw), f"horizontal scroll at {width}px: {sw} > {cw}"
