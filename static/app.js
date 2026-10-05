const $ = id => document.getElementById(id);
const money = n => n == null ? '—' : '₹' + Number(n).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2});
const num = n => n == null ? '—' : Number(n).toLocaleString('en-IN', {maximumFractionDigits:2});
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const signedClass = n => n > 0 ? 'positive' : n < 0 ? 'negative' : '';
let state, token, side='B', selected='', socket, toastTimer, reconnectTimer, busy=false, contractSignature='', connectionError='';
$('connect-form').insertAdjacentHTML('afterbegin','<p id="connect-error" class="connection-form-error" role="alert" hidden></p>');
function connectionMessage(message='') {
  connectionError=message;
  for(const id of ['connection-error','connect-error']) {$(id).textContent=message;$(id).hidden=!message;}
}
function toast(message, error=false) { $('toast').textContent=message; $('toast').className=error?'error':''; $('toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').hidden=true,8000); }
async function api(action, data={}) {
  const response=await fetch('/api/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-Terminal-Token':token},body:JSON.stringify(data)});
  const result=await response.json();
  if(!response.ok){const message=result.error||'Request failed.';if(['connect','expiries','chain','reconcile'].includes(action))connectionMessage(message);throw new Error(message);}
  if(['connect','chain','reconcile'].includes(action))connectionMessage();
  if(result.state)render(result.state);
  return result;
}
async function perform(fn){try{return await fn();}catch(e){toast(e.message,true);}}
function heatmap(){
  const times=['09:18','09:45','10:15','10:45','11:15','11:45','12:15','12:45','13:15','13:45','14:15','14:45'];
  $('heatmap').innerHTML='<thead><tr><th>SL / TIME</th>'+times.map(t=>'<th>'+t+'</th>').join('')+'<th>TOTAL</th></tr></thead><tbody>'+['Strike','Entry','LTP',...Array.from({length:10},(_,i)=>(i+1)*10+'%'),'Total','Best SL','PnL ₹'].map((label,i)=>'<tr class="'+(i<3?'meta-row':i===13?'total-row':i===14?'best-row':'')+'"><td>'+label+'</td>'+times.map(()=>'<td>—</td>').join('')+'<td>—</td></tr>').join('')+'</tbody>';
}
function render(s){
  const chartChanged=!state||[state.mode,state.source,state.selection.underlying].join('|')!==[s.mode,s.source,s.selection.underlying].join('|');
  state=s;
  if(chartChanged)window.dispatchEvent(new Event('terminal-state'));
  const statusError=connectionError||s.connection_error||'';
  $('connection-error').textContent=statusError;$('connection-error').hidden=!statusError;
  $('clock').textContent=new Date(s.time).toLocaleTimeString('en-GB',{timeZone:'Asia/Kolkata'})+' IST';
  $('connection-label').textContent=s.authenticated?(s.connected?'Broker connected':'Broker reconnecting'):'Connect broker';
  $('connection-dot').className=s.connected?'legend-positive':'';
  $('feed-state').textContent=s.source==='demo'?'DEMO · SYNTHETIC':s.connected?'WEBSOCKET':'OFFLINE';
  $('feed-state').className=s.source==='demo'?'muted':s.connected?'positive':'muted';
  $('contracts-count').textContent=s.contracts.length||'—';
  $('dry-mode').classList.toggle('active',s.mode==='dry'); $('live-mode').classList.toggle('active',s.mode==='live');
  $('arm-open').textContent=s.armed?'Disarm live':'Arm live'; $('arm-open').disabled=s.mode!=='live';
  $('halt').textContent=s.halted?'↻ Unlock orders':'■ Lock orders';
  $('start-demo').hidden=s.source==='demo'||s.authenticated;
  $('notice-text').textContent=s.halted?'Order entry locked. Existing broker orders and positions remain open.':s.source==='demo'?'DEMO DATA · Synthetic prices and illustrative lot sizes. Paper trades only; no broker orders.':s.mode==='live'?(s.armed?'LIVE ARMED · Orders submitted here use real funds. TBS status is shown below.':'LIVE DISARMED · Connect the broker and arm live entry before submitting orders.'):s.authenticated?'DRY EXECUTION · Broker WebSocket prices, local simulated fills. No real orders in dry mode.':'Your workspace is ready. Connect Kotak Neo for market data, or explore with synthetic demo prices.';
  document.querySelectorAll('[data-market]').forEach(b=>b.classList.toggle('active',b.dataset.market===s.selection.underlying));
  if(s.source==='demo') $('expiry').innerHTML='<option value="DEMO">Demo expiry</option>';
  else if(!s.authenticated) $('expiry').innerHTML='<option value="">Connect to load</option>';
  else if(s.selection.expiry){
    if(![...$('expiry').options].some(o=>o.value===s.selection.expiry))$('expiry').add(new Option(s.selection.expiry,s.selection.expiry));
    $('expiry').value=s.selection.expiry;
  }
  $('pnl-mode').textContent=s.mode==='dry'?'PAPER':'LIVE';
  const realized=s.session_pnl?.realized??null, unknown=s.session_pnl?.unrealized==null;
  const unrealized=s.session_pnl?.unrealized||0;
  for(const [id,v] of [['net-pnl',realized+unrealized],['realized',realized],['unrealized',unrealized]]){
    $(id).textContent=(unknown&&id!=='realized')?'—':money(v); $(id).className=signedClass(v);
  }
  $('pnl-note').textContent=!s.session_pnl?'Update ready · restart after strategy exit':unknown?'Today · waiting for fresh prices':'Today IST · before brokerage & taxes';
  $('open-count').textContent=(s.session_pnl?.open||0)+' today’s open contracts';
  $('chain-badge').textContent=s.source==='demo'?'Synthetic feed':s.contracts.length?'Broker WebSocket':'Waiting for feed';
  $('chain-empty').hidden=s.contracts.length>0;
  const rows=new Map();
  s.contracts.forEach(c=>{if(!rows.has(c.strike))rows.set(c.strike,{});rows.get(c.strike)[c.option_type]=c;});
  const premium=c=>c?'<button class="'+(c.stale?'stale':'')+'" data-contract="'+esc(c.key)+'" title="'+(c.stale?'Stale quote':'Open order ticket')+'">'+num(c.ltp)+'</button>':'—';
  const change=c=>c&&c.change!=null?'<span class="'+signedClass(c.change)+'">'+num(c.change)+'%</span>':'—';
  $('chain-body').innerHTML=[...rows.entries()].sort((a,b)=>a[0]-b[0]).map(([strike,r])=>'<tr><td>'+num(r.CE?.oi)+'</td><td>'+change(r.CE)+'</td><td>'+premium(r.CE)+'</td><td class="strike">'+num(strike)+'</td><td>'+premium(r.PE)+'</td><td>'+change(r.PE)+'</td><td>'+num(r.PE?.oi)+'</td></tr>').join('');
  const signature=s.contracts.map(c=>c.key).join(',');
  if(contractSignature!==signature){
    contractSignature=signature;
    $('contract').innerHTML='<option value="">Select an option from the chain</option>'+s.contracts.map(c=>'<option value="'+esc(c.key)+'">'+esc(c.symbol)+'</option>').join('');
    if(s.contracts.some(c=>c.key===selected))$('contract').value=selected;else selected='';
  }
  $('ticket-mode').textContent=s.mode.toUpperCase(); $('ticket-mode').className=s.mode==='dry'?'dry-badge':'dry-badge live-badge';
  $('live-confirm-wrap').hidden=s.mode!=='live';
  $('place-order').innerHTML=(s.mode==='dry'?'Place dry order':'Place live order')+' <span>→</span>';
  $('place-order').disabled=busy||s.halted||!selected||(s.mode==='live'&&!s.armed);
  $('execution-note').textContent=s.mode==='dry'?'Paper execution only. Uses marketable limits with 0.05% simulated slippage. Fees are excluded.':'Sends a real MIS limit order. Submission is not a fill. Follow its status in the order book.';
  $('positions-badge').textContent=s.mode==='dry'?'Paper ledger':'Broker quantities';
  if(s.mode==='dry'){
    $('positions-head').innerHTML='<tr><th>CONTRACT</th><th>NET QTY</th><th>AVG ENTRY</th><th>LTP</th><th>REALIZED</th><th>UNREALIZED</th></tr>';
    $('positions-body').innerHTML=s.positions.map(p=>'<tr><td>'+esc(p.symbol)+'</td><td>'+p.qty+'</td><td>'+num(p.average)+'</td><td>'+num(p.ltp)+(p.stale&&p.qty?' *':'')+'</td><td class="'+signedClass(p.realized)+'">'+money(p.realized)+'</td><td class="'+signedClass(p.unrealized)+'">'+(p.stale&&p.qty?'Stale':money(p.unrealized))+'</td></tr>').join('');
    $('positions-empty').hidden=!!s.positions.length;
  }else{
    $('positions-head').innerHTML='<tr><th>CONTRACT</th><th>PRODUCT</th><th>BUY QTY TODAY</th><th>SELL QTY TODAY</th><th>NET QTY + CARRY</th></tr>';
    $('positions-body').innerHTML=s.broker_positions.map(p=>'<tr><td>'+esc(p.trdSym)+'</td><td>'+esc(p.prod)+'</td><td>'+num(p.flBuyQty)+'</td><td>'+num(p.flSellQty)+'</td><td>'+num(Number(p.flBuyQty||0)+Number(p.cfBuyQty||0)-Number(p.flSellQty||0)-Number(p.cfSellQty||0))+'</td></tr>').join('');
    $('positions-empty').hidden=!!s.broker_positions.length;
  }
  $('reconcile').disabled=!s.authenticated;
  $('order-count').textContent=s.orders.length;
  $('orders-body').innerHTML=s.orders.map(o=>'<tr><td>'+esc(o.time.slice(11,19))+'</td><td>'+esc(o.source.toUpperCase())+'</td><td>'+esc(o.symbol)+'</td><td class="'+(o.side==='B'?'positive':'negative')+'">'+(o.side==='B'?'BUY':'SELL')+'</td><td>'+o.qty+'</td><td>'+num(o.price)+'</td><td>'+num(o.fill_price)+'</td><td>'+o.filled_qty+'</td><td><span class="status '+esc(o.status)+'">'+esc(o.status)+'</span></td><td>'+(o.mode==='live'&&o.broker_id&&!['COMPLETE','CANCELLED','REJECTED'].includes(o.status)?'<button class="small outline" data-cancel="'+esc(o.id)+'">Cancel</button>':'')+'</td></tr>').join('');
  $('orders-empty').hidden=!!s.orders.length;
  $('events').innerHTML=s.events.map(e=>'<div class="event"><time>'+esc(e.time.slice(11,19))+'</time><span>'+esc(e.message)+'</span></div>').join('');
  $('saved-wrap').hidden=!s.saved_credentials;
  updateTicket(false);
  if(s.strategy)renderStrategy(s.strategy);
}
function updateTicket(setPrice=false){
  const c=state?.contracts.find(c=>c.key===selected);
  if(setPrice&&c?.ltp){const tick=c.tick_size||.05; $('price').value=((side==='B'?Math.ceil(c.ltp*1.002/tick):Math.floor(c.ltp*.998/tick))*tick).toFixed(2);$('price').step=tick;}
  const qty=c?c.lot_size*Number($('lots').value):null;
  $('ticket-qty').textContent=num(qty);$('ticket-ltp').textContent=num(c?.ltp);$('ticket-value').textContent=qty&&$('price').value?money(qty*Number($('price').value)):'—';
}
function chooseContract(key){selected=key;$('contract').value=key;updateTicket(true);$('place-order').disabled=state.halted||(state.mode==='live'&&!state.armed);}
function showConnection(){$('use-saved').checked=state?.saved_credentials||false;$('credential-fields').hidden=$('use-saved').checked;$('connect-dialog').showModal();}
function connectStream(){
  socket=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws');
  socket.onopen=()=>{$('stream-dot').className='legend-positive';$('stream-label').textContent='Local terminal connected';perform(async()=>{const r=await fetch('/api/bootstrap').then(r=>r.json());token=r.token;});};
  socket.onmessage=e=>render(JSON.parse(e.data));
  socket.onclose=()=>{$('stream-dot').className='';$('stream-label').textContent='Terminal disconnected · reconnecting';$('place-order').disabled=true;clearTimeout(reconnectTimer);reconnectTimer=setTimeout(connectStream,2000);};
}
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-tab]').forEach(x=>x.classList.toggle('nav-active',x===b));['workspace','orders','performance','activity'].forEach(t=>$(t+'-view').hidden=t!==b.dataset.tab);if(b.dataset.tab==='performance')perform(loadPerformance);});
document.querySelectorAll('[data-market]').forEach(b=>b.onclick=()=>perform(async()=>{await api('select',{underlying:b.dataset.market});if(state.authenticated)await loadExpiries();}));
document.querySelectorAll('.close-dialog').forEach(b=>b.onclick=()=>b.closest('dialog').close());
['connect-open','chain-connect'].forEach(id=>$(id).onclick=showConnection);
$('strategy-info').onclick=()=>$('strategy-dialog').showModal();
$('start-demo').onclick=()=>perform(()=>api('demo'));
$('dry-mode').onclick=()=>perform(()=>api('mode',{mode:'dry'}));
$('live-mode').onclick=()=>perform(()=>api('mode',{mode:'live'}));
$('halt').onclick=()=>perform(()=>api(state.halted?'resume':'halt'));
$('arm-open').onclick=()=>state.armed?perform(()=>api('disarm')):$('arm-dialog').showModal();
$('arm-form').onsubmit=e=>{e.preventDefault();perform(async()=>{await api('arm',{phrase:$('arm-phrase').value});$('arm-dialog').close();$('arm-phrase').value='';});};
$('use-saved').onchange=()=>$('credential-fields').hidden=$('use-saved').checked;
async function loadExpiries(){const r=await api('expiries');$('expiry').innerHTML='<option value="">Select expiry</option>'+r.expiries.map(e=>'<option>'+esc(e)+'</option>').join('');if(!r.expiries.length){const message='Broker returned no available expiries for '+state.selection.underlying+'.';connectionMessage(message);throw new Error(message);}$('expiry').value=r.expiries[0];await api('chain',{expiry:r.expiries[0]});}
$('load-chain').onclick=()=>perform(()=>state.authenticated?loadExpiries():Promise.resolve(showConnection()));
$('expiry').onchange=()=>perform(()=>api('chain',{expiry:$('expiry').value}));
$('connect-form').onsubmit=e=>{e.preventDefault();perform(async()=>{const button=$('connect-submit');button.disabled=true;button.textContent='Connecting…';try{await api('connect',{consumer_key:$('consumer-key').value,mobile_number:$('mobile').value,ucc:$('ucc').value,mpin:$('mpin').value,totp:$('totp').value,use_saved:$('use-saved').checked,save:$('save-credentials').checked});['consumer-key','mobile','ucc','mpin','totp'].forEach(id=>$(id).value='');$('connect-dialog').close();toast('Broker authenticated. Loading available expiries…');await loadExpiries();}finally{button.disabled=false;button.textContent='Connect & authenticate ↗';}});};
$('disconnect').onclick=()=>perform(async()=>{await api('disconnect');$('connect-dialog').close();});
$('forget').onclick=()=>perform(async()=>{await api('forget');$('use-saved').checked=false;$('credential-fields').hidden=false;toast('Saved credentials removed.');});
$('reconcile').onclick=()=>perform(()=>api('reconcile'));
$('chain-body').onclick=e=>{const b=e.target.closest('[data-contract]');if(b)chooseContract(b.dataset.contract);};
$('orders-body').onclick=e=>{const b=e.target.closest('[data-cancel]');if(b)perform(()=>api('cancel',{id:b.dataset.cancel}));};
$('contract').onchange=()=>chooseContract($('contract').value);
['lots','price'].forEach(id=>$(id).oninput=()=>updateTicket());
function changeSide(s){side=s;$('buy-side').className=s==='B'?'buy-selected':'';$('sell-side').className=s==='S'?'sell-selected':'';updateTicket(true);}
$('buy-side').onclick=()=>changeSide('B');$('sell-side').onclick=()=>changeSide('S');
$('order-form').onsubmit=e=>{e.preventDefault();if(busy)return;perform(async()=>{busy=true;$('place-order').disabled=true;try{const r=await api('order',{client_id:crypto.randomUUID(),mode:state.mode,key:selected,side,lots:Number($('lots').value),price:Number($('price').value),confirm:$('live-confirm').value});$('live-confirm').value='';toast('Order '+r.order.status.toLowerCase()+(r.order.status==='UNKNOWN'?' — reconcile before another live order.':'.'),r.order.status==='UNKNOWN');}finally{busy=false;render(state);}});};
let strategyAction='';
function renderStrategy(s) {
  document.querySelector('.strategy-value').textContent=s.enabled?'Running':'Paused';
  document.querySelector('.strategy-value').className='strategy-value '+(s.enabled?'positive':'');
  document.querySelector('.strategy-value').nextElementSibling.textContent=s.startup_pending?'First dry entry: waiting for fresh ATM ticks':s.enabled?(s.next_entry?'Next '+s.next_entry+' IST · exit '+s.exit_time:'Entries complete · exit '+s.exit_time):'Exit '+s.exit_time+' · protection remains active';
  $('strategy-status').textContent=s.startup_pending?'Waiting for ATM ticks':s.enabled?'Running · '+state.mode.toUpperCase():'Entries paused';
  $('strategy-start').textContent=state.mode==='dry'?'Start dry TBS':'Start live TBS';
  $('strategy-start').disabled=s.enabled||state.halted||!state.contracts.length;
  $('strategy-demo-entry').hidden=state.source!=='demo';
  $('strategy-demo-entry').disabled=!s.enabled||state.halted;
  $('strategy-lot').textContent='1 lot · '+s.lot_size+' units / leg';
  $('strategy-body').innerHTML=s.tranches.flatMap(t=>t.legs.map(l=>'<tr><td title="'+esc(t.time||'')+'">'+esc(t.startup?t.time.slice(11,19)+' · startup':t.slot)+'</td><td>'+num(t.strike)+'</td><td>'+t.sl+'%</td><td class="'+(l.side==='CE'?'positive':'negative')+'">'+l.side+'</td><td>'+l.qty+'</td><td>'+num(l.entry)+'</td><td>'+num(l.ltp)+'</td><td>'+num(l.trigger)+'</td><td>'+num(l.limit)+'</td><td>'+num(l.exit)+'</td><td><span class="status">'+esc(l.status)+'</span></td><td class="'+signedClass(l.pnl)+'">'+money(l.pnl)+'</td></tr>')).join('');
  $('strategy-empty').hidden=!!s.tranches.length;
  $('strategy-empty').textContent=s.startup_pending?'First dry straddle will enter when the index and both ATM options have fresh prices.':state.mode==='dry'&&state.source==='broker'?'Start dry TBS to enter the first straddle now, then follow the remaining schedule.':'Start TBS to wait for the next scheduled entry. Past live entries are never replayed.';
  $('strategy-error').hidden=!s.error;$('strategy-error').textContent=s.error;
  const ts=s.schedule.map(time=>s.tranches.find(t=>t.slot===time));
  const cell=(v,extra='')=>'<td class="'+extra+'">'+num(v)+'</td>';
  const total=values=>values.some(v=>v===null)?null:values.some(v=>v!=null)?values.reduce((a,v)=>a+(v||0),0):null;
  let html='<thead><tr><th>SL / TIME</th>'+s.schedule.map((t,i)=>'<th>'+esc(ts[i]?.startup?ts[i].time.slice(11,16)+'*':t)+'</th>').join('')+'<th>TOTAL</th></tr></thead><tbody>';
  for(const [name,key] of [['Strike','strike'],['Entry','entry'],['LTP','ltp']])html+='<tr class="meta-row"><td>'+name+'</td>'+ts.map(t=>cell(t?.[key])).join('')+'<td>—</td></tr>';
  const best=ts.map(t=>{const values=Object.entries(t?.scenarios||{}).filter(([,v])=>v!=null);return values.length?values.sort((a,b)=>b[1]-a[1])[0][0]:null;});
  for(let p=10;p<=100;p+=10){const values=ts.map(t=>t?.scenarios[String(p)]);html+='<tr><td>'+p+'%</td>'+values.map((v,i)=>cell(v,(v>0?'heat-profit':v<0?'heat-loss':'')+(best[i]===String(p)?' best-cell':''))).join('')+cell(total(values),signedClass(total(values)))+'</tr>';}
  html+='<tr class="best-row"><td>Best SL*</td>'+best.map(p=>'<td>'+(p?p+'%':'—')+'</td>').join('')+'<td>Hindsight</td></tr>';
  const pnls=ts.map(t=>t?.pnl);html+='<tr class="total-row"><td>Actual PnL ₹</td>'+pnls.map(v=>cell(v,signedClass(v))).join('')+cell(total(pnls),signedClass(total(pnls)))+'</tr></tbody>';$('heatmap').innerHTML=html;
  document.querySelector('.heatmap-panel .subtle-badge').textContent=state.source==='demo'?'Demo paths':'Observed ticks';
  document.querySelector('.heatmap-panel .panel-foot').innerHTML='<span><i class="amber-dot"></i> Heatmap: straddle points · independent leg stops · 2% limit buffer · fees excluded</span><span>'+(s.tranches.some(t=>t.startup)?'* First dry tranche uses its actual startup time. ':'')+'Best SL is hindsight, never an entry signal</span>';
  document.querySelector('footer>span:last-child').textContent='v0.1 · TBS configured';
}
function liveStrategyDialog(action){strategyAction=action;$('strategy-confirm').value='';$('strategy-confirm-action').hidden=false;$('strategy-confirm-label').hidden=false;$('strategy-confirm-action').textContent=action==='strategy'?'Start live TBS':'Exit live TBS';$('strategy-dialog').showModal();}
$('strategy-info').onclick=()=>{$('strategy-confirm-action').hidden=true;$('strategy-confirm-label').hidden=true;$('strategy-dialog').showModal();};
$('strategy-start').onclick=()=>state.mode==='live'?liveStrategyDialog('strategy'):perform(()=>api('strategy'));
$('strategy-pause').onclick=()=>perform(()=>api('strategy-pause'));
$('strategy-exit').onclick=()=>state.mode==='live'?liveStrategyDialog('strategy-exit'):perform(()=>api('strategy-exit'));
$('strategy-demo-entry').onclick=()=>perform(()=>api('strategy-demo-entry'));
$('strategy-confirm-action').onclick=()=>perform(async()=>{await api(strategyAction,{confirm:$('strategy-confirm').value});$('strategy-dialog').close();});
heatmap();
perform(async()=>{const r=await fetch('/api/bootstrap').then(r=>r.json());token=r.token;render(r.state);connectStream();if(r.state.authenticated&&!r.state.contracts.length)await loadExpiries();});

async function loadPerformance(){
  const response=await fetch('/api/performance');
  if(!response.ok){$('performance-empty').hidden=false;$('performance-empty').textContent='Daily performance needs the updated server. Restart after all strategy legs have closed.';return;}
  const data=await response.json();
  const cell=v=>'<td class="'+signedClass(v)+'">'+money(v)+'</td>';
  $('performance-body').innerHTML=data.rows.map(r=>'<tr><td>'+esc(r.date)+'</td><td>'+esc(r.underlying)+'</td><td>'+esc(r.mode==='dry'?'PAPER':'LIVE')+'</td><td>'+esc(r.source.toUpperCase())+'</td>'+cell(r.values.OVERALL.realized)+cell(r.values.OVERALL.unrealized)+cell(r.values.OVERALL.total)+cell(r.values.CE.total)+cell(r.values.PE.total)+'<td>'+esc(r.status)+'</td></tr>').join('');
  $('performance-empty').hidden=!!data.rows.length;$('performance-empty').textContent='No daily performance recorded yet.';
}
$('performance-refresh').onclick=()=>perform(loadPerformance);
