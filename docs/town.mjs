// The data dashboard must continue working even if 3D dependencies are unavailable.

const $ = id => document.getElementById(id);
const fmt = new Intl.NumberFormat('nl-NL',{style:'currency',currency:'EUR',minimumFractionDigits:2,maximumFractionDigits:2});
const pctFmt = new Intl.NumberFormat('nl-NL',{minimumFractionDigits:1,maximumFractionDigits:2});
const esc = x => String(x == null ? '—' : x).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const eur = n => Number.isFinite(Number(n)) ? fmt.format(Number(n)) : '—';
const num = (n,d=0) => Number.isFinite(Number(n)) ? new Intl.NumberFormat('nl-NL',{maximumFractionDigits:d}).format(Number(n)) : '—';
const percent = n => Number.isFinite(Number(n)) ? (Number(n)>0?'+':'')+pctFmt.format(Number(n))+'%' : '—';
const cls = n => Number(n)>0?'good':Number(n)<0?'bad':'muted';
const time = ts => ts ? new Date(Number(ts)*1000).toLocaleString('nl-NL',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'}) : '—';
let DATA=null,rawTimestamp=0,selected='townhall',world=null,period='7d',tick=null,refreshing=false,motion=!matchMedia('(prefers-reduced-motion: reduce)').matches,alertTimeout=null;
const buildings={
 townhall:{title:'Raadshuis',intro:'Het bestuurlijke hart. Hier zie je hoe de hele koopmansstad ervoor staat.',icon:'♛'},
 market:{title:'Marktplaats',intro:'Alle werkelijk uitgevoerde orders en verkopen, zonder verzonnen activiteiten.',icon:'⚖'},
 treasury:{title:'Schatkamer',intro:'Je Kraken-vermogen, beschikbare euro’s en open bezittingen.',icon:'◈'},
 tower:{title:'Wachttoren',intro:'De stadswacht bewaakt stop-orders, verliesgrenzen en storingen.',icon:'♜'},
 guild:{title:'Gildehuis',intro:'Je handelsstrategieën: wie handelt live, wie oefent in schaduw?',icon:'⚒'},
 library:{title:'Bibliotheek',intro:'Onderzoeksresultaten en prestaties, met onderscheid tussen bewijs en verwachting.',icon:'▤'},
 gate:{title:'Stadspoort',intro:'Verkenners bekijken cryptomarkten en brengen kandidaten naar de stad.',icon:'✧'}
};
function tile(name,value,kind=''){return '<div class="tile"><small>'+esc(name)+'</small><b class="'+kind+'">'+esc(value)+'</b></div>'}
function item(title,info,value='',color=''){return '<div class="detail-item"><div><b>'+esc(title)+'</b><small>'+esc(info)+'</small></div><span class="value '+color+'">'+esc(value)+'</span></div>'}
function note(s){return '<div class="copy-note">'+esc(s)+'</div>'}
function section(s){return '<div class="section-title">'+esc(s)+'</div>'}
function riskStatus(d){
 const risk=d?.books?.live?.risk||{};
 if(risk.halted)return ['NOODSTOP ACTIEF','danger','De noodstop heeft nieuwe aankopen geblokkeerd.'];
 if(!risk.trading_enabled)return ['AANKOPEN GEBLOKKEERD','warning','Live handelen staat uit of er is een veiligheidsonderbreking.'];
 if(risk.daily_limit_hit)return ['DAGLIMIET BEREIKT','warning','Nieuwe aankopen zijn voor vandaag geblokkeerd.'];
 return ['LIVE HANDEL ACTIEF','good','De bot mag kopen als de strategie en veiligheidsregels dat toestaan.'];
}
function svgChart(history,key='equity'){
 const h=history.filter(p=>p&&Number.isFinite(Number(p[key]))&&Number.isFinite(Number(p.ts))).slice(-500);
 if(h.length<2)return '<div class="empty">Nog onvoldoende meetpunten voor een koersgrafiek.</div>';
 const now=h[h.length-1].ts,from=period==='1d'?now-86400:period==='7d'?now-7*86400:-Infinity;
 const visible=h.filter(p=>p.ts>=from);
 if(visible.length<2)return '<div class="empty">Nog geen geschiedenis voor deze periode.</div>';
 const vals=visible.map(p=>Number(p[key]));let min=Math.min(...vals),max=Math.max(...vals);
 let span=max-min||Math.max(max*.01,.1);min-=span*.17;max+=span*.17;
 let x=i=>i/(vals.length-1)*300,y=v=>120-(v-min)/(max-min)*108;
 let coordinates=vals.map((v,i)=>(i?'L':'M')+x(i).toFixed(1)+' '+y(v).toFixed(1)).join(' ');
 let area=coordinates+' L 300 126 L 0 126 Z';
 let c=vals[vals.length-1]>=vals[0]?'#7cdbad':'#ed8f8a';
 return '<svg class="chart" role="img" aria-label="Verloop van je Kraken-vermogen" viewBox="0 0 300 135" preserveAspectRatio="none">'+
 '<path d="'+area+'" fill="'+(c==='#7cdbad'?'#386c5a':'#724951')+'" opacity=".25"/>'+
 [35,70,105].map(y=>'<line x1="0" y1="'+y+'" x2="300" y2="'+y+'" stroke="#67839a" stroke-opacity=".2"/>').join('')+
 '<path d="'+coordinates+'" fill="none" stroke="'+c+'" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>'+
 '<div class="legend"><span>'+esc(time(visible[0].ts))+'</span><span>→</span><span>'+esc(time(visible[visible.length-1].ts))+'</span></div>';
}
function chartBlock(hist){return '<div class="tabbar" aria-label="Periode"><button data-period="1d" class="'+(period==='1d'?'active':'')+'">24 UUR</button><button data-period="7d" class="'+(period==='7d'?'active':'')+'">7 DAGEN</button><button data-period="all" class="'+(period==='all'?'active':'')+'">ALLES</button></div>'+svgChart(hist)}
function riskCards(live,d){
 const r=live.risk||{},limits=d.risk_limits||{};
 const dd=Number(r.drawdown_pct)||0,maxDD=Number(limits.max_drawdown_pct)||20;
 const loss=Number(r.daily_loss_pct)||0,maxLoss=Number(limits.max_daily_loss_pct)||5;
 return section('Waakzaamheid')+
 '<div class="tile-grid">'+tile('Drawdown',percent(dd),dd>maxDD*.8?'bad':'')+tile('Verlies vandaag',percent(loss),loss>maxLoss*.8?'bad':'')+
 tile('Daglimiet',percent(maxLoss))+tile('Maximale drawdown',percent(maxDD))+'</div>'+
 '<div class="healthline"><span class="dot '+(r.halted?'bad':r.daily_limit_hit?'warn':'')+'"></span>'+(r.halted?'Noodstop actief':r.daily_limit_hit?'Daglimiet bereikt':'Risicobewaking ingeschakeld')+'</div>';
}
function viewTownhall(d){
 const b=d.books?.live||{},stats=b.stats||{},sh=d.books?.schaduw||{},[label,code,message]=riskStatus(d),history=b.equity_history||[];
 return '<div class="tile-grid">'+tile('Totaal vermogen',eur(stats.equity))+tile('Rendement',percent(stats.return_pct),cls(stats.return_pct))+
 tile('Vrije euro’s',eur(b.cash))+tile('Open posities',num(b.positions?.length||0))+'</div>'+
 section('Vermogensverloop')+chartBlock(history)+
 section('Stadsbericht')+
 '<div class="healthline"><span class="dot '+(code==='danger'?'bad':code==='warning'?'warn':'')+'"></span><b>'+esc(label)+'</b></div>'+
 note(message)+
 section('Activiteit')+
 item('Schaduwposities', 'Gesimuleerde investeringen', num(sh.positions?.length||0))+
 item('Live afgesloten trades','In werkelijkheid verkochte posities',num(b.trades?.length||0))+
 item('Marktfase',d.regime?.reason||'Niet beschikbaar',d.regime?.label||'—')+
 '<div class="copy-note">Alle getoonde bedragen zijn afkomstig uit het laatste botrapport. De stad is een visualisatie, geen tweede handelsmotor.</div>';
}
function viewTreasury(d){
 const b=d.books?.live||{},s=b.stats||{},hold=b.positions||[],total=(Number(b.cash)||0)+(Number(b.exposure)||0);
 let html='<div class="tile-grid">'+tile('Kraken-vermogen',eur(s.equity))+tile('Beschikbaar',eur(b.cash))+
 tile('Belegd',eur(b.exposure))+tile('Resultaat sinds start',eur(s.realized_pnl),cls(s.realized_pnl))+'</div>';
 html+=section('Verdeling');
 let exposed=total>0?clipPercent(100*(Number(b.exposure)||0)/total):0;
 html+='<div class="progress"><div style="width:'+exposed.toFixed(1)+'%"></div></div>'+
 '<div class="legend"><span>Belegd: '+percent(exposed)+'</span><span>Vrij: '+percent(100-exposed)+'</span></div>';
 html+=section('Bezittingen op Kraken');
 if(!hold.length)html+='<div class="empty">Geen open live cryptoposities. Het saldo staat momenteel vrij.</div>';
 for(const p of hold)html+=item(p.market||'Crypto',num(p.qty,7)+' stuks · instap '+eur(p.entry_price),
 eur((p.qty||0)*(p.last_price||p.entry_price||0)));
 html+=section('Boekhouding')+item('Gerealiseerde winst/verlies','Afgesloten live trades',eur(s.realized_pnl),cls(s.realized_pnl))+
 item('Betaalde kosten','Door bot geregistreerd',eur(s.fees_paid))+
 item('Startkapitaal','Basis voor gemeten rendement',eur(s.start));
 return html;
}
function clipPercent(x){return Math.min(100,Math.max(0,x))}
function viewMarket(d){
 const b=d.books?.live||{},positions=b.positions||[],history=(b.trades||[]).slice().sort((a,b)=>(b.exit_ts||0)-(a.exit_ts||0));
 const events=(d.events||[]).filter(e=>e.book==='live'&&(e.type==='entry'||e.type==='exit')).sort((a,b)=>(b.ts||0)-(a.ts||0));
 let html='<div class="tile-grid">'+tile('Live posities',num(positions.length))+tile('Afgesloten trades',num(history.length))+
 tile('Gerealiseerd resultaat',eur(b.stats?.realized_pnl),cls(b.stats?.realized_pnl))+tile('Nu belegd',eur(b.exposure))+'</div>';
 html+=section('Uitgevoerde bewegingen');
 if(!events.length&&!history.length)html+='<div class="empty">Nog geen geregistreerde echte aankopen of verkopen.</div>';
 for(const e of events.slice(0,7))html+=item((e.type==='entry'?'Gekocht: ':'Verkocht: ')+(e.market||'Onbekend'),time(e.ts)+' · '+(e.agent||'Strategie'),
 e.pnl!=null?eur(e.pnl):e.price!=null?eur(e.price):'',cls(e.pnl));
 if(!events.length)for(const t of history.slice(0,6))html+=item('Gesloten: '+t.market,time(t.exit_ts),eur(t.pnl),cls(t.pnl));
 html+=section('Open orders');
 html+='<div class="empty">Deze publieke dashboardfeed bevat geen volledig overzicht van open Kraken-orders. Voor daadwerkelijke orderstatus is Kraken zelf leidend.</div>';
 return html;
}
const DIRECTION={'-2':'Sterk negatief','-1':'Negatief','0':'Neutraal','1':'Positief','2':'Sterk positief'};
function direction(v){return DIRECTION[String(Math.max(-2,Math.min(2,Math.round(Number(v)||0))))]}
function newsBlock(d){
 const n=d.news;
 if(!n)return section('Nieuwsbode')+'<div class="empty">De nieuws-agent heeft nog niet gedraaid.</div>';
 const ai=n.ai||{},u=ai.used_today||{},caps=ai.caps||{},now=Number(d.generated_ts||0);
 const aiOn=Boolean(ai.token&&!ai.paused_until&&Number(ai.last_ok||0)>now-3*3600);
 let html=section('Nieuwsbode')+
 '<div class="healthline"><span class="dot '+(aiOn?'':'warn')+'"></span><b>'+(aiOn?'AI leest het nieuws mee':'Woordenlijst bewaakt het nieuws')+'</b></div>'+
 note(aiOn?'Elk kwartier beoordeelt een gratis AI-model de nieuwe koppen. Meldt het nieuws een hack, delisting of rechtszaak, dan koopt de bot die munt 48 uur niet.'
  :(ai.paused_until?'De gratis AI is tot '+time(ai.paused_until)+' niet beschikbaar. ':'De AI heeft nog geen beoordeling gegeven. ')+'Tot dan houdt een woordenlijst hard slecht nieuws tegen.')+
 '<div class="tile-grid">'+tile('Beoordelingen vandaag',num(u.light||0)+' van '+num(caps.light||0))+tile('Koopchecks vandaag',num(u.heavy||0)+' van '+num(caps.heavy||0))+
 tile('Fear & Greed',n.fng?num(n.fng.value)+' '+(n.fng.label||''):'—')+tile('Nieuws over de markt',n.market?direction(n.market.richting):'—')+'</div>';
 if(n.market&&n.market.reden)html+=note(n.market.reden);
 const vetoes=n.vetoes||[];
 html+=section('Niet kopen door nieuws');
 html+=vetoes.length?vetoes.map(v=>item(v.market,(v.reason||'')+' (tot '+time(v.until)+')','VETO','bad')).join('')
  :'<div class="empty">Geen hard slecht nieuws over de munten die we volgen.</div>';
 const sc=n.scorecard||{},t=sc.trades||{};
 html+=section('Koopcheck')+note(n.koopcheck_live?'De koopcheck heeft zijn doel gehaald en houdt nu aankopen tegen.'
  :'De koopcheck geeft bij elke koopkans een tweede mening, maar telt pas mee als hij bewezen verliezers eruit haalt: minstens 1 op de 5 verliezers, en hooguit 1 op de 10 winnaars.')+
 item('Gecheckte trades','Afgesloten trades met een tweede mening',num(t.gecheckt||0)+' van '+num(t.nodig||50))+
 item('Verliezers tegengehouden','Had de check deze verliezers tegengehouden?',num(t.verliezers_tegengehouden||0)+' van '+num(t.verliezers||0),t.verliezers?'good':'')+
 item('Winnaars tegengehouden','Had de check deze winnaars ten onrechte tegengehouden?',num(t.winnaars_tegengehouden||0)+' van '+num(t.winnaars||0),t.winnaars_tegengehouden?'bad':'');
 const pass=sc.koopcheck_doorlaten||{},stop=sc.koopcheck_tegenhouden||{};
 if((pass.gemeten||0)+(stop.gemeten||0)>0)html+=item('Koers 24 uur later','Doorgelaten tegen tegengehouden',percent(pass.r24)+' / '+percent(stop.r24));
 const coins=(n.coins||[]).filter(c=>Number(c.richting)!==0).slice(0,6);
 if(coins.length){html+=section('Laatste oordelen');for(const c of coins)html+=item(c.base,c.reden||c.title||'',direction(c.richting),cls(c.richting))}
 if(n.last_error&&Number(n.last_error_ts||0)>now-3600)html+='<div class="copy-note">Laatste storing: '+esc(n.last_error)+'. De bot handelt gewoon door.</div>';
 return html;
}
function viewTower(d){
 const l=d.books?.live||{},r=l.risk||{},p=d.risk_limits||{};
 const [label,code,message]=riskStatus(d);
 let html='<div class="healthline"><span class="dot '+(code==='danger'?'bad':code==='warning'?'warn':'')+'"></span><b>'+esc(label)+'</b></div>'+
 note(message)+riskCards(l,d);
 html+=section('Positiegrenzen')+
 item('Risico per trade','Basispercentage',percent(p.risk_per_trade_pct))+
 item('Max. positiegrootte','Aandeel van het vermogen',percent(p.max_position_pct))+
 item('Max. totale blootstelling','Alle posities samen',percent(p.max_exposure_pct))+
 item('Max. open posities','Tegelijkertijd',num(p.max_open_positions));
 html+=section('Stop-loss-controle');
 const pos=l.positions||[];
 if(!pos.length)html+='<div class="empty">Er zijn geen open live posities die een stop-loss nodig hebben.</div>';
 for(const x of pos){
 const missing=!x.stop_order_id||x.protection_pending;
 html+=item(x.market,missing?'Exchange-stop onbevestigd':'Stop-order geregistreerd',
 missing?'CONTROLE':'BEWAAKT',missing?'bad':'good');
 }
 html+=newsBlock(d);
 if((d.errors||[]).length)html+=section('Gemelde fouten')+(d.errors||[]).slice(0,4).map(x=>item(time(x.ts),x.message,'!','bad')).join('');
 return html;
}
function viewGuild(d){
 const agents=d.agents||[];
 const active=agents.filter(a=>a.status==='live');
 let html='<div class="tile-grid">'+tile('Gilden totaal',num(agents.length))+tile('Handelen live',num(active.length))+
 tile('In schaduw',num(agents.length-active.length))+tile('Marktfase',d.regime?.label||'—')+'</div>';
 html+=section('De gildeleden');
 if(!agents.length)html+='<div class="empty">Nog geen strategiegegevens ontvangen.</div>';
 for(const a of agents){
 const label=a.agent||a.name||'Strategie';const live=a.status==='live';
 html+=item(label,(a.horizon||'')+' · '+(a.promotion_note?.reason||a.promotion_note||''),
 (live?'LIVE':'SCHADUW')+' · '+num(a.trades)+' trades',live?'good':'amber');
 }
 html+=note('Alleen een gilde dat in de bron als live staat, mag echte orders voorstellen. De risicowacht heeft altijd het laatste woord.');
 return html;
}
function viewLibrary(d){
 let html='<div class="tile-grid">'+tile('Strategieën',num(d.agents?.length||0))+tile('Shadow trades',num(d.books?.schaduw?.trades?.length||0))+
 tile('Schaduwrendement',percent(d.books?.schaduw?.stats?.return_pct),cls(d.books?.schaduw?.stats?.return_pct))+
 tile('Winstkans live',percent(d.books?.live?.stats?.win_rate_pct))+'</div>';
 html+=section('Onderzoeksdossiers');
 let found=false;
 for(const a of d.agents||[]){
 const bt=a.backtest;
 if(!bt||typeof bt!=='object'||!Object.keys(bt).length)continue;
 found=true;
 const oos=bt.oos||bt;const r=oos.expectancy_r??oos.oos_expectancy_r??bt.oos_expectancy_r;
 const trades=oos.trades??oos.oos_trades;
 html+=item(a.agent||a.name||'Strategie',trades!=null?num(trades)+' historische testtrades':'Backtest beschikbaar',
 r!=null?num(r,3)+' R':'Bekijk Details',cls(r));
 }
 if(!found)html+='<div class="empty">Geen uitgewerkte backtestresultaten in deze dashboardfeed.</div>';
 html+=note('Een backtest is geen bewijs van toekomstig rendement. Oude en nieuwe testmethoden kunnen verschillen.');
 html+=section('Verdiep je verder')+
 '<div class="detail-item"><div><b>Backtests en strategieanalyse</b><small>Volledige onderzoeken en historie.</small></div><a class="value" href="details.html">Open ↗</a></div>';
 return html;
}
function viewGate(d){
 const s=d.scanner||{},candidates=s.candidates||[],active=s.active_markets||d.markets||[];
 let html='<div class="tile-grid">'+tile('Markten gezien',num(s.eligible_count))+tile('Kandidatenpool',num(s.pool_count))+
 tile('Actief gevolgd',num(active.length))+tile('Nieuwe ontdekkingen',num(s.active_discovery_markets?.length||s.discovery_markets?.length||0))+'</div>';
 html+=section('Verkenners op pad')+note(s.shadow_only?'Nieuwe discovery-markten worden voorlopig alleen op papier getest.':'Verkenning volgt de huidige botregels.');
 html+=section('Markten die opvallen');
 if(!candidates.length)html+='<div class="empty">Geen marktkandidaten beschikbaar in de huidige scan.</div>';
 for(const c of candidates.slice(0,9))
 html+=item(c.market||'Markt', 'Verandering 24u: '+percent(c.change_pct)+' · volume '+eur(c.quote_volume),
 c.score!=null?num(Number(c.score)*100,1)+'/100':'—',c.selected?'good':'');
 const picks=d.news?.picks||[];
 html+=section('Kansen uit het nieuws');
 html+=picks.length?note('Munten met een concrete nieuwe aanleiding. Ze volgen de regel van de verkenners'+(s.shadow_only?': eerst alleen op papier.':'.'))+
  picks.map(p=>item(p.market,p.reason||'Nieuwe aanleiding','tot '+time(p.until),'good')).join('')
  :'<div class="empty">Het nieuws bracht nu geen nieuwe kansen.</div>';
 html+=section('Nu in de stad')+'<div class="copy-note">'+esc(active.slice(0,15).join(' · ')||'Nog geen markten')+'</div>';
 return html;
}
const renderers={townhall:viewTownhall,market:viewMarket,treasury:viewTreasury,tower:viewTower,guild:viewGuild,library:viewLibrary,gate:viewGate};
function selectBuilding(key,focus=false){
 if(!buildings[key])return;selected=key;
 document.querySelectorAll('[data-select]').forEach(el=>el.classList.toggle('active',el.dataset.select===key));
 if(world&&focus)world.select(key,true);
 if(!world){document.querySelectorAll('.place-label').forEach(el=>el.classList.toggle('active',el.dataset.select===key));}
 const cfg=buildings[key];$('detail-title').textContent=cfg.icon+' '+cfg.title;$('detail-copy').textContent=cfg.intro;
 $('detail-content').innerHTML=DATA?renderers[key](DATA):'<div class="empty">Kraken-data wordt geladen…</div>';
 document.querySelectorAll('[data-period]').forEach(btn=>btn.addEventListener('click',()=>{
 period=btn.dataset.period;selectBuilding(selected,false);
 }));
 if(focus&&innerWidth<901){$('detail-title').scrollIntoView({behavior:'smooth',block:'nearest'})}
}
function renderBottom(d){
 const ev=(d.events||[]).slice().sort((a,b)=>(b.ts||0)-(a.ts||0)).slice(0,5);
 $('recent-events').innerHTML=ev.length?ev.map(e=>{
  let label=e.type==='entry'?'AANKOOP':e.type==='exit'?'VERKOOP':e.type==='blocked'?'GEBLOKKEERD':e.type==='validated'?'VALIDATIE':'BERICHT';
  const market=e.market?e.market+' · ':'',name=market+(e.reason||label);
  return '<div class="board-row"><span class="time">'+esc(time(e.ts))+'</span><div><b class="'+(e.type==='exit'?'bad':e.type==='entry'?'good':'')+'">'+label+'</b><div><small>'+esc(name).slice(0,130)+'</small></div></div><div class="value">'+esc(e.book==='live'?'LIVE':'TEST')+'</div></div>';
 }).join(''):'<div class="empty">Nog geen gebeurtenissen.</div>';
 const live=d.books?.live||{},p=live.positions||[];
 $('positions-summary').innerHTML=p.length?p.slice(0,6).map(x=>'<div class="board-row two"><div><b>'+esc(x.market)+'</b><div><small>'+esc(num(x.qty,7))+' stuks · gekocht voor '+esc(eur(x.entry_price))+'</small></div></div><span class="value">'+esc(eur((x.qty||0)*(x.last_price||x.entry_price||0)))+'</span></div>').join(''):
 '<div class="board-row two"><div><b>Geen open posities</b><div><small>De bot kan bij een nieuw, geldig koopsignaal weer investeren.</small></div></div><span class="value">'+eur(live.cash)+'</span></div>';
 const agents=d.agents||[];
 $('guild-summary').innerHTML=agents.slice(0,6).map(a=>'<div class="board-row two"><div><b>'+esc(a.agent||a.name)+'</b><div><small>'+esc(a.horizon||'Strategie')+' · '+esc(num(a.trades))+' trades</small></div></div><span class="value '+(a.status==='live'?'good':'amber')+'">'+esc(a.status==='live'?'LIVE':'SCHADUW')+'</span></div>').join('')||
 '<div class="empty">Geen strategiegegevens.</div>';
}
function updateHead(d){
 const live=d.books?.live||{},s=live.stats||{},r=live.risk||{};
 $('h-equity').textContent=eur(s.equity);
 $('h-cash').textContent=eur(live.cash);
 $('h-positions').textContent=num(live.positions?.length||0);
 $('h-return').textContent=percent(s.return_pct);
 $('h-return').className=Number(s.return_pct)>=0?'positive':'negative';
 const delta=Date.now()/1000-Number(d.generated_ts||0);
 const old=delta>900,failed=!d.last_run_ok,risk=r.halted||!r.trading_enabled;
 const health=$('h-health');
 health.className='health '+(old||failed?'danger':risk?'warning':'good');
 health.textContent=old?'● DATA VERTRAAGD':failed?'● BOTMELDING':risk?'● LIVE BEPERKT':'● LIVE ACTIEF';
 $('h-sync').textContent=d.generated_ts?time(d.generated_ts):'Geen update';
 $('data-age').textContent='Botupdate: '+time(d.generated_ts)+(old?' (vertraagd)':'');
 $('city-mode').textContent=d.mode==='live'?'ECHT GELD':'SIMULATIE';
 $('city-subtitle').textContent=(d.markets?.length||0)+' markten · '+(d.regime?.label||'onbekend')+' markt';
 $('label-tower')?.classList.toggle('has-alert',Boolean(old||failed||r.halted||(d.errors||[]).some(e=>Number(e.ts||0)>Number(d.generated_ts||0)-86400)));
 $('label-gate')?.classList.toggle('has-alert',Boolean(d.scanner?.enabled&&d.scanner?.active_discovery_markets?.length));
}
function updateAll(d){
 DATA=d;updateHead(d);renderBottom(d);selectBuilding(selected,false);
 const latestLive=(d.events||[]).filter(e=>(e.type==='entry'||e.type==='exit')&&e.book==='live').sort((a,b)=>(b.ts||0)-(a.ts||0))[0];
 if(latestLive&&rawTimestamp&&Number(latestLive.ts)>rawTimestamp){
  const button=$('label-market');button.classList.add('active');setTimeout(()=>{if(selected!=='market')button.classList.remove('active')},5000);
  notify('📯 '+(latestLive.type==='entry'?'Echte aankoop':'Echte verkoop')+': '+latestLive.market);
 }
 rawTimestamp=Math.max(rawTimestamp,Number(d.generated_ts)||0,Number(latestLive?.ts)||0);
}
function notify(message){
 const el=$('toast');el.textContent=message;el.classList.add('visible');clearTimeout(alertTimeout);alertTimeout=setTimeout(()=>el.classList.remove('visible'),6500);
}
async function loadData(){
 if(refreshing)return;refreshing=true;
 try{
  const res=await fetch('data.json?ts='+Date.now(),{cache:'no-store'});
  if(!res.ok)throw Error('HTTP '+res.status);
  const d=await res.json();
  if(!d.books?.live||!d.generated_ts)throw Error('Ongeldig Kraken-datarapport');
  if(!DATA||d.generated_ts!==DATA.generated_ts){updateAll(d)}
  else updateHead(d);
 }catch(err){
  $('h-health').textContent='● GEEN VERBINDING';$('h-health').className='health danger';$('h-sync').textContent='Datafout';
  if(!DATA)$('detail-content').innerHTML='<div class="empty">De Kraken-databron kon niet geladen worden: '+esc(err.message)+'</div>';
 }finally{refreshing=false}
}
function bindControls(){
 document.querySelectorAll('[data-select]').forEach(el=>el.addEventListener('click',()=>selectBuilding(el.dataset.select,true)));
 $('home').addEventListener('click',()=>{selectBuilding('townhall',false);world?.reset()});
 $('close-detail').addEventListener('click',()=>{selectBuilding('townhall',false);world?.reset()});
 $('camera-home').addEventListener('click',()=>world?.reset());
 $('zoom-in').addEventListener('click',()=>world?.zoom(.22));
 $('zoom-out').addEventListener('click',()=>world?.zoom(-.22));
 $('animation-toggle').addEventListener('click',e=>{
 motion=!motion;e.currentTarget.setAttribute('aria-pressed',String(motion));e.currentTarget.textContent=motion?'▶':'Ⅱ';world?.animate(motion);
 });
 window.addEventListener('keydown',e=>{
  if(['INPUT','TEXTAREA'].includes(document.activeElement?.tagName))return;
  if(e.key==='Escape'){selectBuilding('townhall',false);world?.reset()}
  if(e.key==='+'||e.key==='=')world?.zoom(.16);
  if(e.key==='-')world?.zoom(-.16);
 });
}
function fallback(){
 const map=$('fallback-map');map.classList.add('visible');$('world-wrap').classList.add('no-webgl');
 const spots={
  townhall:[49,34],market:[48,63],treasury:[79,36],
  tower:[18,28],guild:[20,56],library:[77,62],gate:[50,86]
 };
 for(const [key,coords] of Object.entries(spots)){
 const label=$('label-'+key);
 label.style.left=coords[0]+'%';label.style.top=coords[1]+'%';
 }
 notify('3D-renderer niet beschikbaar. Gebouwen zijn nog steeds aanklikbaar.');
}
bindControls();
selectBuilding('townhall',false);
loadData();
setInterval(loadData,45000);
(async()=>{
 try{
  const {initTown}=await import('./town-world.mjs');
  world=initTown($('world'),key=>selectBuilding(key,false));
  world.animate(motion);
 }catch(e){
  console.error('Koopmansstad 3D could not start',e);
  fallback();
 }
})();
