/* Observed P&L only: no interpolating across missing quotes or feed outages. */
(() => {
  const $ = id => document.getElementById(id);
  const fmt = value => value == null ? '—' : '₹' + value.toLocaleString('en-IN', {maximumFractionDigits: 2});
  const time = stamp => new Date(stamp * 1000).toLocaleTimeString('en-GB', {timeZone:'Asia/Kolkata',hour:'2-digit',minute:'2-digit',second:'2-digit'});
  let data, side = 'OVERALL', geometry, pending = false;
  const svg = $('pnl-chart'), tooltip = $('pnl-tooltip');
  function draw() {
    if (!data) return;
    const current = data.current[side], points = data.points;
    $('chart-total').textContent = 'Total: ' + fmt(current.total);
    $('chart-total').className = current.total > 0 ? 'positive' : current.total < 0 ? 'negative' : '';
    $('chart-context').textContent = `${data.underlying} · ${data.date} · ${data.source === 'demo' ? 'DEMO' : data.mode === 'dry' ? 'PAPER' : 'LIVE'} · CE ${data.current.CE.open} / PE ${data.current.PE.open} open`;
    $('chart-note').textContent = current.total == null ? 'Waiting for fresh prices · missing marks appear as gaps' : 'Terminal fills today · before fees · history sampled every 5 seconds';
    const lines = ['total', ...($('pnl-realized').checked ? ['realized'] : []), ...($('pnl-unrealized').checked ? ['unrealized'] : [])];
    const values = points.flatMap(p => lines.map(k => p[side][k])).filter(v => v != null);
    $('chart-empty').hidden = values.length > 0;
    $('chart-empty').textContent = points.length ? 'Waiting for fresh marks. Enable Realized to see confirmed closed P&L.' : 'History begins with observed fills. No historical prices are invented.';
    const width = Math.max(360, svg.clientWidth), height = 280, left = 16, right = width - 88, top = 15, bottom = 245;
    let low = Math.min(0,...values), high = Math.max(0,...values);
    const pad = Math.max((high-low)*.12, 100); low -= pad; high += pad;
    const first = points[0]?.time || 0, last = Math.max(first+60, points.at(-1)?.time || 0);
    const x = t => left + (t-first)/(last-first)*(right-left), y = v => bottom-(v-low)/(high-low)*(bottom-top), zero = y(0);
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    let html = `<defs><clipPath id="pnl-positive"><rect x="${left}" y="${top}" width="${right-left}" height="${zero-top}"/></clipPath><clipPath id="pnl-negative"><rect x="${left}" y="${zero}" width="${right-left}" height="${bottom-zero}"/></clipPath></defs>`;
    for (let i=0;i<=4;i++) {
      const v = low+(high-low)*i/4, yy = y(v);
      html += `<line class="pnl-grid" x1="${left}" x2="${right}" y1="${yy}" y2="${yy}"/><text x="${right+8}" y="${yy+4}">${Math.round(v).toLocaleString('en-IN')}</text>`;
    }
    html += `<line class="pnl-zero" x1="${left}" x2="${right}" y1="${zero}" y2="${zero}"/>`;
    // Split paths at missing marks and at sampling interruptions longer than 15s.
    for (const key of lines) {
      const segments = []; let segment = [];
      for (const p of points) {
        if (p[side][key] == null || (segment.length && p.time-segment.at(-1).time > 15)) {
          if (segment.length) segments.push(segment); segment=[];
        }
        if (p[side][key] != null) segment.push(p);
      }
      if (segment.length) segments.push(segment);
      for (const segment of segments) {
        const path = segment.map((p,i)=>`${i?'L':'M'}${x(p.time)},${y(p[side][key])}`).join(' ');
        if (key === 'total') {
          const area = `${path} L${x(segment.at(-1).time)},${zero} L${x(segment[0].time)},${zero} Z`;
          html += `<path d="${area}" fill="#28ce7b" opacity=".07" clip-path="url(#pnl-positive)"/><path d="${area}" fill="#ff496e" opacity=".1" clip-path="url(#pnl-negative)"/>`;
          for (const [clip,color] of [['positive','#28ce7b'],['negative','#ff496e']]) html += `<path d="${path}" fill="none" stroke="${color}" stroke-width="2" clip-path="url(#pnl-${clip})"/>`;
          if (segment.length===1) html += `<circle cx="${x(segment[0].time)}" cy="${y(segment[0][side][key])}" r="2" fill="${segment[0][side][key]>=0?'#28ce7b':'#ff496e'}"/>`;
        } else html += `<path d="${path}" fill="none" stroke="${key==='realized'?'#39a9e8':'#dca64a'}" stroke-width="1.2" stroke-dasharray="4 3"/>`;
      }
    }
    if(points.length) for(let i=0;i<=4;i++) {const t=first+(last-first)*i/4; html+=`<text x="${x(t)}" y="270" text-anchor="${i===0?'start':i===4?'end':'middle'}">${last-first<300?time(t):time(t).slice(0,5)}</text>`;}
    if(current.total!=null && values.length) html+=`<line x1="${left}" x2="${right}" y1="${y(current.total)}" y2="${y(current.total)}" stroke="${current.total>=0?'#28ce7b':'#ff496e'}" stroke-dasharray="2 3" opacity=".6"/>`;
    html += '<line id="pnl-crosshair" class="pnl-zero" y1="15" y2="245" visibility="hidden"/>';
    svg.innerHTML = html; geometry = {first,last,left,right,width};
  }
  svg.addEventListener('pointermove', e => {
    if (!data?.points.length || !geometry) return;
    const rect=svg.getBoundingClientRect(), px=(e.clientX-rect.left)*geometry.width/rect.width;
    const stamp=geometry.first+(px-geometry.left)/(geometry.right-geometry.left)*(geometry.last-geometry.first);
    const p=data.points.reduce((a,b)=>Math.abs(a.time-stamp)<Math.abs(b.time-stamp)?a:b), value=p[side];
    tooltip.textContent=`${time(p.time)} IST · Total ${fmt(value.total)} · Realized ${fmt(value.realized)} · Unrealized ${fmt(value.unrealized)}`;
    tooltip.hidden=false;
    const line=$('pnl-crosshair'), xx=geometry.left+(p.time-geometry.first)/(geometry.last-geometry.first)*(geometry.right-geometry.left);
    line.setAttribute('x1',xx);line.setAttribute('x2',xx);line.setAttribute('visibility','visible');
  });
  svg.addEventListener('pointerleave',()=>{tooltip.hidden=true;$('pnl-crosshair')?.setAttribute('visibility','hidden');});
  document.querySelectorAll('[data-pnl-side]').forEach(button => button.onclick = () => {
    side=button.dataset.pnlSide;
    document.querySelectorAll('[data-pnl-side]').forEach(b=>{b.classList.toggle('active',b===button);b.setAttribute('aria-pressed',String(b===button));});draw();
  });
  ['pnl-realized','pnl-unrealized'].forEach(id=>$(id).onchange=draw);
  new ResizeObserver(draw).observe(svg);
  async function refresh() {
    if(pending) return; pending=true;
    try {const response=await fetch('/api/pnl');if(!response.ok)throw Error();data=await response.json();draw();}
    catch {$('chart-note').textContent='P&L history unavailable · reconnecting';}
    finally {pending=false;}
  }
  window.addEventListener('terminal-state', refresh);
  refresh();setInterval(refresh,5000);
})();
