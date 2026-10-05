let brokerData=null, watchId='', watchNew=false, brokerLoading=false, lastBrokerConnection=null, searchResults=[];
const stampLabel=value=>value?'Updated '+new Date(value*1000).toLocaleTimeString('en-GB',{timeZone:'Asia/Kolkata'})+' IST':'Awaiting refresh';
function brokerRender(data){
  brokerData=data;
  const funds=data.funds.data||{}, holdings=data.holdings.data||[], portfolio=data.portfolio;
  const stale=value=>!value||Date.now()/1000-value>60;
  $('broker-status').textContent=data.connected?'Broker account':'Disconnected';
  $('broker-status').className='subtle-badge '+(data.connected?'positive':'');
  $('broker-refresh').disabled=!data.connected||brokerLoading;
  $('broker-connect').hidden=data.connected;
  const errors=[data.funds.error,data.holdings.error,data.positions_error,data.quote_error].filter(Boolean);
  $('broker-message').textContent=!data.connected?'Connect Kotak Neo with your current TOTP to load funds, holdings and account watchlists.':errors.length?errors.join(' '):stale(data.funds.updated_at)?'Account data is awaiting refresh. Last available balances may be stale.':'Broker account overview · funds and holdings refresh every 30 seconds while this tab is open. Positions reconcile every 10 seconds.';
  $('broker-message').classList.toggle('negative',!!errors.length);
  for(const [id,value] of [['available',funds.available],['margin',funds.margin_used],['value',portfolio.value],['profit',portfolio.pnl],['collateral',funds.collateral],['payin',funds.pay_in],['payout',funds.pay_out],['realized',funds.realized],['unrealized',funds.unrealized]]){
    $('broker-'+id).textContent=money(value);
    if(['profit','realized','unrealized'].includes(id))$('broker-'+id).className=signedClass(value);
  }
  $('broker-cost').textContent='Invested value: '+money(portfolio.cost);
  $('broker-funds-time').textContent=stampLabel(data.funds.updated_at)+(data.funds.error||stale(data.funds.updated_at)?' · stale':'');
  $('broker-holdings-count').textContent=data.holdings.data?holdings.length+' holdings':'Awaiting holdings';
  $('broker-open').textContent=data.connected?portfolio.open+' open contracts':'—';
  $('broker-positions-time').textContent=stampLabel(data.positions_updated_at)+(stale(data.positions_updated_at)?' · stale':'');
  $('broker-holdings-time').textContent=stampLabel(data.holdings.updated_at)+(data.holdings.error||stale(data.holdings.updated_at)?' · stale':'');
  $('broker-positions-body').innerHTML=data.positions.map(p=>'<tr><td>'+esc(p.symbol)+'</td><td>'+esc(p.product)+'</td><td>'+num(p.qty)+'</td><td>'+num(p.ltp)+'</td><td class="'+signedClass(p.pnl)+'">'+money(p.pnl)+'</td><td><span class="status">'+esc(p.status)+'</span></td></tr>').join('');
  $('broker-positions-empty').hidden=!!data.positions.length;
  $('broker-positions-empty').textContent=data.connected?'No broker positions.':'Connect to load broker positions.';
  $('broker-holdings-body').innerHTML=holdings.map(p=>'<tr><td>'+esc(p.symbol)+'</td><td>'+num(p.qty)+'</td><td>'+num(p.sellable)+'</td><td>'+num(p.average)+'</td><td>'+money(p.value)+'</td><td class="'+signedClass(p.pnl)+'">'+money(p.pnl)+'</td></tr>').join('');
  $('broker-holdings-empty').hidden=!!holdings.length;
  $('broker-holdings-empty').textContent=data.holdings.error?'Holdings unavailable. Refresh to retry.':data.holdings.data?'No demat holdings in this account.':'Connect to load your holdings.';
  if(!data.watchlists.some(w=>w.id===watchId))watchId=data.watchlists[0]?.id||'';
  $('watch-select').innerHTML=data.watchlists.length?data.watchlists.map(w=>'<option value="'+esc(w.id)+'">'+esc(w.name)+'</option>').join(''):'<option value="">No watchlists</option>';
  $('watch-select').value=watchId;
  const current=watchNew?null:data.watchlists.find(w=>w.id===watchId);
  if(document.activeElement!==$('watch-name'))$('watch-name').value=current?.name||'';
  for(const id of ['watch-new','watch-name','watch-select','watch-exchange','watch-query'])$(id).disabled=!data.connected;
  $('watch-delete').disabled=!current;
  $('watch-search-form').querySelector('button').disabled=!current;
  $('watch-name-form').querySelector('button').disabled=!data.connected;
  $('watch-body').innerHTML=(current?.items||[]).map(i=>'<tr><td class="watch-symbol">'+esc(i.symbol)+'<small>'+esc(i.exchange.toUpperCase())+'</small></td><td title="'+esc(stampLabel(i.quote?.updated_at))+'">'+num(i.quote?.ltp)+(stale(i.quote?.updated_at)?' *':'')+'</td><td><button class="outline small" data-watch-remove="'+esc(i.key)+'" aria-label="Remove '+esc(i.symbol)+'">×</button></td></tr>').join('');
  $('watch-empty').hidden=!!current?.items.length;
  $('watch-message').textContent=!data.connected?'Connect to access your saved account watchlists.':watchNew?'Name your new watchlist and click Save.':current?'Search instruments to add. * Quote unavailable or older than 60 seconds.':'Create your first watchlist to get started.';
}
async function loadBroker(force=false){
  if(brokerLoading)return;
  brokerLoading=true;$('broker-refresh').disabled=true;
  try{
    let data;
    if(force){const result=await api('terminal-refresh');data=result.terminal;}
    else {const response=await fetch('/api/broker-terminal');if(!response.ok)throw new Error('Could not load broker account.');data=await response.json();}
    brokerRender(data);
  }catch(error){$('broker-message').textContent=error.message;$('broker-message').classList.add('negative');}
  finally{brokerLoading=false;$('broker-refresh').disabled=!brokerData?.connected;}
}
function currentWatch(){return brokerData?.watchlists.find(w=>w.id===watchId);}
async function saveWatch(data){const result=await api('watchlist-save',data);watchNew=false;brokerRender(result.terminal);}
$('broker-refresh').onclick=()=>loadBroker(true);
$('broker-connect').onclick=showConnection;
$('watch-new').onclick=()=>{watchNew=true;searchResults=[];$('watch-results').hidden=true;brokerRender(brokerData);$('watch-name').focus();};
$('watch-select').onchange=()=>{watchNew=false;watchId=$('watch-select').value;$('watch-results').hidden=true;brokerRender(brokerData);};
$('watch-name-form').onsubmit=e=>{e.preventDefault();perform(async()=>{const before=new Set(brokerData.watchlists.map(w=>w.id));await saveWatch({id:watchNew?undefined:watchId||undefined,name:$('watch-name').value});const added=brokerData.watchlists.find(w=>!before.has(w.id));if(added)watchId=added.id;brokerRender(brokerData);});};
$('watch-delete').onclick=()=>perform(async()=>{const result=await api('watchlist-delete',{id:watchId});watchId='';$('watch-results').hidden=true;brokerRender(result.terminal);});
$('watch-search-form').onsubmit=e=>{e.preventDefault();perform(async()=>{const button=$('watch-search-form').querySelector('button');button.disabled=true;try{const result=await api('terminal-search',{exchange:$('watch-exchange').value,query:$('watch-query').value});searchResults=result.results;$('watch-results').hidden=false;$('watch-results').innerHTML=searchResults.length?searchResults.map((r,index)=>'<button type="button" data-watch-add="'+index+'">'+esc(r.symbol)+' <small>'+esc(r.exchange)+' · + Add</small></button>').join(''):'<p class="empty compact">No matching instruments.</p>';}finally{button.disabled=!currentWatch();}});};
$('watch-results').onclick=e=>{const b=e.target.closest('[data-watch-add]');if(!b)return;perform(async()=>{const w=currentWatch(),item=searchResults[Number(b.dataset.watchAdd)];if(!w||!item)return;if(w.items.some(i=>i.key===item.key)){toast('Already in this watchlist.');return;}await saveWatch({id:w.id,items:[...w.items,item]});toast('Added '+item.symbol);await loadBroker(true);});};
$('watch-body').onclick=e=>{const b=e.target.closest('[data-watch-remove]');if(b)perform(()=>saveWatch({id:watchId,items:currentWatch().items.filter(i=>i.key!==b.dataset.watchRemove)}));};
document.querySelector('[data-tab="broker"]').addEventListener('click',()=>loadBroker());
window.addEventListener('broker-state',()=>{
  if(lastBrokerConnection!==state.authenticated){lastBrokerConnection=state.authenticated;brokerData=null;watchId='';watchNew=false;searchResults=[];$('watch-results').hidden=true;if(!$('broker-view').hidden)loadBroker();}
});
setInterval(()=>{if(!$('broker-view').hidden&&!document.hidden)loadBroker();},15000);
