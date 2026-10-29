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
  const fail=[];const ok=(c,m)=>{if(!c)fail.push(m)};const vis=n=>n&&n.offsetParent!==null;
  try{
    // landing: the group with the largest per-day impact, never one that needs no change
    const L=G();ok(S.view==='group'&&!L.no_action_needed,'landed on a no-change group');
    D.groups.forEach(x=>{if(!x.no_action_needed)ok(Math.abs(usdOf(L))>=Math.abs(usdOf(x))-1e-9,'landing is not the largest impact: '+x.model+' '+x.role)});
    ok(location.hash.startsWith('#g='),'hash not set');
    // display names: date suffix only in the tooltip
    ok(label('claude-haiku-4-5-20251001')==='Haiku 4.5'&&family('claude-haiku-4-5-20251001')==='Haiku','claude label');
    ok(label('claude-opus-5-5')==='Opus 5.5'&&label('gpt-5-codex')==='GPT-5-codex','labels');
    // chart first, then profiles, KPIs, table
    const ids=[...document.querySelectorAll('#fMain,#fImpact,#kpis,#fCmp')].map(n=>n.id).join();ok(ids==='fMain,fImpact,kpis,fCmp','order '+ids);
    const n0=document.getElementById('rng');const g=D.groups[S.gi];const max=g.grid.length-1;
    ok(n0&&n0.closest('#fMain'),'slider is not under the chart');
    n0.focus();
    for(const v of [0,3,Math.min(7,max),2,max,1]){
      n0.value=v;n0.dispatchEvent(new Event('input',{bubbles:true}));
      ok(document.getElementById('rng')===n0,'slider node replaced after input '+v);
      ok(S.T===g.grid[v],'state not updated for '+v);
      ok(document.getElementById('tval').textContent===fk(g.grid[v]),'value label stale at '+v);
    }
    ok(document.activeElement===n0,'slider lost focus');
    ok(document.querySelector('[data-gear=custom]').getAttribute('aria-pressed')==='true','custom not active');
    // drag on the chart: the cursor follows, the chart and slider nodes stay
    const svg=document.querySelector('#fMain svg.mainc'),ov=svg.querySelector('rect.drag'),r0=ov.getBoundingClientRect();
    const ev=(type,fx)=>ov.dispatchEvent(new PointerEvent(type,{bubbles:true,pointerId:1,clientX:r0.left+r0.width*fx,clientY:r0.top+r0.height/2}));
    ev('pointerdown',.004);const tA=S.T;ev('pointermove',.5);ev('pointermove',.996);const tB=S.T;ev('pointerup',.996);
    ok(tA===g.grid[0]&&tB===g.grid[max],'drag did not move the threshold '+tA+' -> '+tB);
    ok(document.querySelector('#fMain svg.mainc')===svg&&document.getElementById('rng')===n0,'chart or slider rebuilt during drag');
    ok(+n0.value===max&&svg.querySelector('.chl').textContent===fk(S.T),'slider or cursor label not synced with the drag');
    document.querySelector('[data-gear=time]').click();
    ok(document.getElementById('rng')===n0,'slider node replaced after profile click');
    ok(+n0.value===g.grid.indexOf(g.profiles.time.threshold),'slider not moved to the time profile');
    ok(document.querySelector('[data-gear=time]').getAttribute('aria-pressed')==='true','time not active');
    const def=document.querySelector('.mk[data-ref=rDefault]');ok(def,'default mark missing');
    if(def){def.click();ok(S.gear==='custom'&&document.getElementById('rng')===n0,'default mark did not snap')}
    const pm=document.querySelector('.mk[data-g=balanced]');ok(pm,'balanced mark missing');
    if(pm){pm.click();ok(S.gear==='balanced','balanced mark did not select the profile')}
    // one answer line with a copy button; savings carry ▼ and no minus sign
    {const im=impJS(pt(g,g.grid.indexOf(S.T)),g.default);const a=document.getElementById('ans').textContent;
      // percent first (the default unit), the per-day amount small after it
      const pd=pctAt(g,S.T,g.default);ok(S.unit==='pct'&&a.startsWith(fpct(pd.c)+' cost')&&a.includes(fsn(im.usd.per_day))&&/per day vs default/.test(a),'answer line: '+a);
      // every delta carries its arrow and its sign: '▼ −$x' saves, '▲ +$x' adds; never a bare '−$x' or '▼ $x'
      document.querySelectorAll('#fImpact [data-m] .ld').forEach(c=>ok(/^(\\$0|0 min|0%|[▼] −|[▲] \\+)/.test(c.textContent.trim()),'unsigned delta chip: '+c.textContent));
      // the lead figures carry arrow and sign; the small secondary figures carry the sign only
      document.querySelectorAll('#ans>span:not(.cap):not(.ans2)').forEach(x=>ok(/^[▼▲] [−+]/.test(x.textContent),'answer lead sign: '+x.textContent));
      ok(!/per day per day/.test(a),'answer line repeats per day');
      ok(document.getElementById('cpOne').disabled===(action(g,S.T,S.vot)!=='aSet'),'copy button state');
      ok(!!g.current===!document.querySelector('#ans .ans2.cap'),'current-setting note');
      ok(!!g.current===!!document.querySelector('#fMain .mcur'),'current mark')}
    ok(!document.querySelector('#kpis .kh .ic use[href="#i-coin"]'),'money KPI repeats the cards');
    {const mb=document.querySelector('#mini .minib');ok(mb&&/was .* now /.test(mb.dataset.tip)&&mb.querySelector('svg[aria-label]'),'before/after pair on the model page')}
    // a warning triangle in the bar only for the selected profile's guardrail flags
    const want=g.profiles.balanced.flags.filter(f=>!NOTE(f)).length,warn=document.querySelectorAll('#flags .ib.low,#flags .ib.bad').length;
    ok(warn===want,'bar shows '+warn+' warnings for flags '+g.profiles.balanced.flags.join(','));
    const b=document.querySelector('#fCopy .seg[data-k=profile] button[data-v=time]');
    b.focus();b.click();
    ok(document.activeElement&&document.activeElement.dataset.v==='time','copy panel lost focus');
    // hover tooltip on the cost chart
    const hv=document.querySelector('#fCost rect.hov');ok(hv,'no hover layer on the cost chart');
    if(hv){const r=hv.getBoundingClientRect();
      const at={bubbles:true,clientX:r.left+r.width/2,clientY:r.top+r.height/2};
      hv.dispatchEvent(new PointerEvent('pointermove',at));
      const tip=document.getElementById('tip');ok(!tip.hidden&&/k/.test(tip.textContent),'tooltip not shown');
      // rows name their basis: the window total is a plain amount, the per-day rows are signed deltas
      ok(/Window cost \\(total\\)\\$[\\d,]/.test(tip.textContent)&&/Δ cost vs default([▼▲] [−+]\\d|0%)/.test(tip.textContent)&&/Δ time vs default/.test(tip.textContent),'tooltip rows: '+tip.textContent)}
    // per-day impact: three profile cards; the money cell shows the stored vs-default number for the chosen basis
    ok(document.querySelectorAll('#fImpact .pc').length===3,'impact cards missing');
    {const im=g.comparison.balanced.vs_default.impact;
      const cell=()=>document.querySelector('#fImpact [data-pc=balanced] [data-m=usd-vs_default]'),lead=()=>cell().querySelector('.ld').textContent,sec=()=>cell().querySelector('.sc').textContent;
      // % leads (engine's d_cost_pct) with the per-day amount small; the $ toggle swaps them
      ok(lead()===fpct(g.comparison.balanced.vs_default.d_cost_pct)&&sec()===fsn(im.usd.per_day),'percent lead '+lead()+' / '+sec());
      document.querySelector('#units [data-unit=usd]').click();
      const money=lead,n1=document.getElementById('rng');
      ok(S.unit==='usd'&&money()===fsv(im.usd.per_day)&&sec()===fpn(g.comparison.balanced.vs_default.d_cost_pct),'per-day money '+money()+' != '+fsv(im.usd.per_day));
      document.querySelector('#fImpact [data-basis=per_month]').click();
      ok(money()===fsv(im.usd.per_month),'per-month money not shown');
      const bb=document.querySelector('#fImpact [data-basis=per_active_day]');
      ok(!!bb===Math.abs(g.active_days-Math.max(g.days,1))>=.05,'per-active-day toggle shown although it equals per day');
      if(bb){bb.focus();bb.click();ok(money()===fsv(im.usd.per_active_day),'per-active-day money not shown');
        ok(document.activeElement&&document.activeElement.dataset.basis==='per_active_day','basis lost focus')}
      ok(document.getElementById('rng')===n1,'slider replaced by the impact panel');
    }
    // every icon is either hidden from assistive tech or named; every button has an accessible name
    document.querySelectorAll('svg.ic').forEach((s,i)=>ok(s.getAttribute('aria-hidden')==='true'||s.getAttribute('aria-label'),'unnamed icon #'+i));
    document.querySelectorAll('button').forEach(b=>ok((b.getAttribute('aria-label')||b.textContent).trim(),'button without a name: '+b.outerHTML.slice(0,80)));
    // info / warning icons show their text as a tooltip on keyboard focus
    const info=document.querySelector('#iImpact .ib');info.focus();
    ok(!document.getElementById('tip').hidden&&document.getElementById('tip').textContent===info.getAttribute('aria-label'),'info tooltip not shown on focus');
    // one language per label
    const zh=(document.getElementById('main').innerText.match(/[\u4e00-\u9fff]+/g)||[]);ok(!zh.length,'Chinese text in the English view: '+zh.slice(0,5).join(','));
    // a guardrail that only moved the pick is a note (info icon), never a warning
    for(const k of ['cost','balanced','time']){const warn=document.querySelectorAll(`#fImpact [data-pc=${k}] .ib.low, #fImpact [data-pc=${k}] .ib.bad`).length;
      const want=g.profiles[k].flags.filter(f=>!['guardrail_applied','deep_recall_below_floor'].includes(f)).length;
      ok(warn===want,k+': '+warn+' warning icons for flags '+g.profiles[k].flags.join(','))}
    // theme toggle sets data-theme on <html>
    document.querySelector('#themes [data-th=dark]').click();
    ok(document.documentElement.dataset.theme==='dark','dark theme not applied');
    document.querySelector('#themes [data-th=auto]').click();
    ok(!document.documentElement.dataset.theme,'auto theme not restored');
    // the control bar stays visible while scrolling
    scrollTo(0,document.body.scrollHeight);
    ok(Math.abs(document.getElementById('bar').getBoundingClientRect().top)<1,'control bar is not sticky');
    scrollTo(0,0);
    // the same rows in Chinese
    document.querySelector('#langs [data-l=zh]').click();
    {const hv2=document.querySelector('#fCost rect.hov'),r=hv2.getBoundingClientRect();hv2.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,clientX:r.left+r.width/2,clientY:r.top+r.height/2}));
      const tx=document.getElementById('tip').textContent;ok(/窗口总成本/.test(tx)&&/较默认费用/.test(tx)&&/较默认耗时/.test(tx),'zh tooltip rows: '+tx)}
    document.querySelector('#langs [data-l=en]').click();
    // sidebar: harness > family > model; folds toggle and are remembered; search filters
    ok(document.querySelector('#tree .har .fam .mrow.cur .mn[aria-current=page]'),'selected model not in the tree');
    const hf=document.querySelector('#tree .har>.fold');hf.click();
    ok(hf.getAttribute('aria-expanded')==='false'&&hf.nextElementSibling.hidden&&S.fold[hf.dataset.fold]===false,'fold did not collapse');hf.click();
    const tq=document.getElementById('tq');tq.value='zzzz';tq.dispatchEvent(new Event('input'));ok(!document.querySelector('#tree .mrow'),'search did not filter');
    tq.value='';tq.dispatchEvent(new Event('input'));ok(document.querySelector('#tree .mrow'),'search did not reset');
    // overview: one bar per model that needs a change, one table row per group
    document.querySelector('#tree [data-ov]').click();
    ok(S.view==='overview'&&vis(document.getElementById('fOv'))&&!vis(document.getElementById('fMain')),'overview not shown');
    const nAct=new Set(D.groups.filter(x=>!x.no_action_needed).map(x=>x.harness+'|'+x.model)).size;
    // before / after: one row per model that is not "no change", plus a total row when any model is "set", in both panels
    const nSet=new Set(D.groups.filter(x=>isSet(x)).map(x=>x.harness+'|'+x.model)).size,nRows=nAct+(nSet?1:0);
    for(const id of ['ovC','ovM'])ok(document.querySelectorAll('#'+id+' .barow').length===nRows,id+' rows '+document.querySelectorAll('#'+id+' .barow').length+' != '+nRows);
    ok(document.querySelectorAll('#ovSR li').length===nRows&&/per day/.test(document.getElementById('ovSR').textContent),'screen-reader rows');
    // keep-default rows draw only the original bar
    document.querySelectorAll('#ovC .barow.keep').forEach(r=>ok(!r.querySelector('.ban'),'keep-default row has a "now" bar'));
    // the total sums the "set" models' was and now (never averaged percents)
    if(nSet){const ms={};D.groups.forEach(x=>{const key=x.harness+'|'+x.model;(ms[key]=ms[key]||[]).push(x)});
      const sets=Object.values(ms).filter(gs=>gs.some(x=>isSet(x))),was=sets.flat().reduce((a,x)=>a+defUsd(x),0),now=was+sets.flat().reduce((a,x)=>a+setUsd(x),0);
      const tl=document.querySelector('#ovC .barow.tot tspan.big').textContent.trim();const want=S.unit==='pct'?fpct((now-was)/was):fsv(now-was)+t('uDay');ok(tl===want,'total row '+tl+' != '+want)}
    // a model that gets dearer sticks out past its original bar in amber
    {const gi=D.groups.findIndex(x=>isSet(x));if(gi>=0){const im=impOf(D.groups[gi],S.op,'vs_default'),keepU=im.usd.per_day;im.usd.per_day=Math.abs(keepU)+5;
      renderOverview();ok(document.querySelector('#ovC .baa'),'no amber overshoot for an added cost');im.usd.per_day=keepU;renderOverview()}}
    // each row = 100%: every model's original bar has the same length; the choice is remembered
    document.querySelector('[data-ovs=norm]').click();
    {const ws=[...document.querySelectorAll('#ovC .barow:not(.tot) .baw')].map(r=>+r.getAttribute('width'));ok(S.ovScale==='norm'&&ws.every(w=>Math.abs(w-ws[0])<.5),'normalised widths '+ws.join(','));
      let sv=null;try{sv=localStorage.getItem('compactlab.ovScale')}catch(e){}ok(sv==null||sv==='norm','scale not remembered')}
    document.querySelector('[data-ovs=abs]').click();
    // chart style: dumbbells by default from nine rows up, bars below; the toggle overrides and is remembered
    ok(S.ovStyle==null&&ovStyleFor(9)==='dots'&&ovStyleFor(8)==='bars','auto chart style');
    ok(document.querySelector('#ovC svg.bac').classList.contains(ovStyleFor(nRows)),'auto style not applied');
    document.querySelector('[data-ovst=dots]').click();
    {const sv2=document.querySelectorAll('#ovC svg.bac.dots .barow');ok(sv2.length===nRows,'dumbbell rows');
      ok(document.querySelectorAll('#ovC .dwas').length===nRows&&document.querySelectorAll('#ovC .dnow').length===nRows-document.querySelectorAll('#ovC .barow.keep').length,'was / now dots');
      ok(document.querySelectorAll('#ovM svg.bac.dots').length===1&&document.querySelectorAll('#ovSR li').length===nRows,'time panel or screen-reader rows in dumbbell style');
      let sv=null;try{sv=localStorage.getItem('compactlab.ovStyle')}catch(e){}ok(sv==null||sv==='dots','style not remembered')}
    document.querySelector('[data-ovst=bars]').click();ok(document.querySelector('#ovC svg.bac.bars .baw'),'bars style back');
    ok(document.querySelectorAll('#ovT tr').length===D.groups.length+1,'roll-up rows');
    // the headline total counts only groups whose action is "set"
    // totals: summed amounts over summed defaults, never averaged percents (the $ unit is still on from above)
    {const want=D.groups.reduce((a,x)=>a+setUsd(x),0),v=document.querySelector('#ovw .kpi .v').textContent;
      ok(v.startsWith(fsv(want)),'overview total '+v+' != '+fsv(want));
      document.querySelector('#units [data-unit=pct]').click();
      const wp=want/baseUsd(D.groups),vp=document.querySelector('#ovw .kpi .v .ld').textContent;
      ok(S.unit==='pct'&&vp===fpct(wp),'overview percent '+vp+' != '+fpct(wp));
      let unitSaved=null;try{unitSaved=localStorage.getItem('compactlab.unit')}catch(e){}ok(unitSaved==null||unitSaved==='pct','unit not remembered');
      // the table sorts by a column header
      const sb=document.querySelector('#ovT [data-sort=cUsd]');sb.click();
      const vals=[...document.querySelectorAll('#ovT tr')].slice(1).map(r=>D.groups[+r.querySelector('[data-tab]').dataset.tab]).filter(x=>!x.no_action_needed).map(x=>pctAt(x,x.profiles[S.op].threshold,x.default).c);
      ok(vals.every((v,i)=>!i||v>=vals[i-1]-1e-12),'table not sorted by cost %');S.ovSort=null;keep(renderOverview);
      ok(D.groups.some(x=>!x.no_action_needed&&!isSet(x))?document.querySelector('#ovC .barow.keep'):true,'keep-default rows not greyed')}
    ok(/^All sessions/.test(document.getElementById('hSess').textContent),'overview sessions title');
    // one base: the cards' percent equals the chart's Total row, for cost and time, and the Overview badge agrees
    {const pc=document.querySelector('#ovw .kpi .v .ld').textContent,tc=document.querySelector('#ovC .barow.tot tspan.big');
      const pm=document.querySelectorAll('#ovw .kpi .v .ld')[1].textContent,tm=document.querySelector('#ovM .barow.tot tspan.big');
      ok(S.unit==='pct'&&tc&&pc===tc.textContent.trim()&&tm&&pm===tm.textContent.trim(),'card vs Total row: '+pc+' / '+(tc&&tc.textContent)+' · '+pm+' / '+(tm&&tm.textContent));
      const ob=document.querySelector('#tree [data-ov] .bdg').firstChild.textContent;ok(ob.replace('▼−','▼ −').replace('▲+','▲ +')===pc,'Overview badge '+ob+' != '+pc);
}
    // a "keep default" row says which rule kept it
    D.groups.forEach((x,i)=>{const P=x.profiles[S.op];if(action(x,P.threshold,P.value_of_time)!=='aKeep')return;
      const tr=document.querySelector(`#ovT [data-tab="${i}"]`).closest('tr');ok(tr.querySelector('.kw')&&/keep default: /.test(tr.querySelector('.kw').textContent),'keep-default row without a reason: '+x.model)});
    // a no-change group: a short state and the peaks chart, no empty cards, no warning triangles
    const ni=D.groups.findIndex(x=>x.no_action_needed);ok(ni>=0,'fixture has no no-change group');
    if(ni>=0){const nb=document.querySelector(`#tree [data-tab="${ni}"]`);if(nb)nb.click();else pick(ni);
      ok(S.view==='noop'&&vis(document.querySelector('#fNoop svg')),'no-change view not shown');
      ok(![...document.querySelectorAll('.pc,.kpi')].some(vis),'empty cards shown for a no-change group');
      ok(![...document.querySelectorAll('#main .ib.low,#main .ib.bad')].some(vis),'warning icon on a no-change group');
      const ng=D.groups[ni];ok(D.sessions.filter(x=>x.model===ng.model&&x.role===ng.role).length===document.querySelectorAll('#st tr').length-1,'sessions not filtered to the group');
      // the sidebar reason uses this role's own peak
      const row=[...document.querySelectorAll('#tree .why')].find(w=>w.closest('.mrow').querySelector(`[data-tab="${ni}"]`));
      if(row)ok(row.textContent.includes(fk(ng.peak_max)),'no-change reason '+row.textContent+' lacks '+fk(ng.peak_max))}
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
        [chrome, "--headless=new", "--use-mock-keychain", "--password-store=basic", "--disable-gpu", "--no-first-run",
         f"--user-data-dir={tmp_path / 'prof'}",
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
        [chrome, "--headless=new", "--use-mock-keychain", "--password-store=basic", "--disable-gpu", "--no-first-run",
         "--allow-file-access-from-files",
         f"--user-data-dir={tmp_path / 'prof'}", "--virtual-time-budget=3000", "--window-size=1500,1000",
         "--dump-dom", host.as_uri()],
        capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    sw, cw = out.stdout.split('data-sw="')[1].split('"')[0].split("/")
    assert int(sw) <= int(cw), f"horizontal scroll at {width}px: {sw} > {cw}"


ONE_BASE = """
<script>
(function(){
  const fail=[];const ok=(c,m)=>{if(!c)fail.push(m)};
  try{
    ok(D.groups.some(x=>x.no_action_needed)&&D.groups.some(x=>!x.no_action_needed&&!isSet(x)),'fixture lacks no-op or keep-default groups');
    overview();
    const card=[...document.querySelectorAll('#ovw .kpi .v .ld')].map(n=>n.textContent),tot=['#ovC','#ovM'].map(q=>document.querySelector(q+' .barow.tot tspan.big').textContent.trim());
    ok(card[0]===tot[0]&&card[1]===tot[1],'cards '+card.join(',')+' != Total rows '+tot.join(','));
    const sets=D.groups.filter(x=>setModel(x)),was=sets.reduce((a,x)=>a+defUsd(x),0),saved=D.groups.reduce((a,x)=>a+setUsd(x),0);
    ok(card[0]===fpct(saved/was),'cost % is not saving / default of the set models: '+card[0]+' vs '+fpct(saved/was));
    const ob=document.querySelector('#tree [data-ov] .bdg').firstChild.textContent.replace('▼−','▼ −').replace('▲+','▲ +');ok(ob===card[0],'Overview badge '+ob);
    // harness badge: the same base over that harness's groups
    const h=D.groups[0].harness,hg=D.groups.filter(x=>x.harness===h),hp=hg.reduce((a,x)=>a+setUsd(x),0)/baseUsd(hg);
    const hb=document.querySelector(`#tree [data-fold="h:${h}"] .bdg`).firstChild.textContent.replace('▼−','▼ −').replace('▲+','▲ +');
    ok(hb===fpct(hp),'harness badge '+hb+' != '+fpct(hp));
  }catch(e){fail.push('error: '+e.message)}
  document.body.dataset.selftest=fail.length?'fail: '+fail.join(' | '):'ok';
})();
</script>
"""


@pytest.mark.skipif(_chrome() is None, reason="no Chrome/Chromium for the DOM check")
def test_overview_percent_has_one_base(tmp_path):
    """Cards, the chart's Total rows and the sidebar badges share one base, on data with keep-default and no-op."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "make_assets", Path(__file__).parent.parent / "scripts" / "make_assets.py"
    )
    assert spec and spec.loader
    ma = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ma)
    res = ma.showcase(tmp_path / "showcase")
    page = tmp_path / "r.html"
    page.write_text(render_html(res).replace("</body>", ONE_BASE + "</body>"))
    out = subprocess.run(
        [_chrome(), "--headless=new", "--use-mock-keychain", "--password-store=basic", "--disable-gpu", "--no-first-run",
         f"--user-data-dir={tmp_path / 'prof'}", "--dump-dom", page.as_uri()],
        capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert 'data-selftest="ok"' in out.stdout, out.stdout.split("data-selftest=")[-1][:400]
