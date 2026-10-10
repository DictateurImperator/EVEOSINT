  const chart=document.getElementById('pe-chart'),tip=document.getElementById('pe-chart-tooltip');
  const chartStatus=document.getElementById('pe-chart-status');
  const metric=document.getElementById('pe-chart-metric'),basis=document.getElementById('pe-chart-basis');
  let points=[],viewStart=0,viewEnd=0,chartController=null,drag=null;
  const colors={npc_bounties_isk:'#c084fc',mining_isk:'#4ade80',production_isk:'#60a5fa',npc_bounties_ppa_isk:'#d8b4fe',mining_ppa_isk:'#86efac',production_ppa_isk:'#93c5fd',isk_purchasing_power_index:'#fbbf24',consumer_price_index_relative_index:'#f87171'};
  const unitCaption=()=>metric.value.endsWith('_index')?'Index (reference '+document.getElementById('pe-reference').value+' = 100)':metric.value.includes('_ppa_')?'PPA ISK (reference '+document.getElementById('pe-reference').value+')':'ISK';
  const escape=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function values(){return points.slice(viewStart,viewEnd+1).map(p=>p.values[metric.value]==null?null:Number(p.values[metric.value]));}
  function bounds(){const nums=values().filter(Number.isFinite);if(!nums.length)return null;let min=Math.min(...nums),max=Math.max(...nums);const pad=min===max?Math.max(1,Math.abs(min)*.05):(max-min)*.08;return [min-pad,max+pad];}
  function hideTip(){tip.hidden=true;const mark=document.getElementById('pe-chart-marker');if(mark)mark.setAttribute('visibility','hidden');}
  function renderEvolution(){
    hideTip();chart.dataset.viewStart=String(viewStart);chart.dataset.viewEnd=String(viewEnd);
    const range=bounds();if(!range){chart.innerHTML='<text x="500" y="180" text-anchor="middle" fill="#94a3b8">No numeric data for this range.</text>';return;}
    const [min,max]=range,visible=points.slice(viewStart,viewEnd+1),nums=values();
    const x=i=>visible.length===1?523:74+i/(visible.length-1)*898,y=v=>24+(max-v)/(max-min)*288;
    let svg='';for(let i=0;i<=4;i++){const v=max-i/4*(max-min),yy=24+i/4*288;svg+='<line x1="74" x2="972" y1="'+yy+'" y2="'+yy+'" stroke="rgba(148,163,184,.12)"/><text x="64" y="'+(yy+4)+'" text-anchor="end" fill="#64748b" font-size="11">'+escape(compact(v))+'</text>';}
    const ticks=Math.min(6,visible.length);for(let i=0;i<ticks;i++){const idx=ticks===1?0:Math.round(i/(ticks-1)*(visible.length-1));svg+='<text x="'+x(idx)+'" y="342" text-anchor="middle" fill="#64748b" font-size="11">'+escape(visible[idx].month)+'</text>';}
    let path='',started=false;nums.forEach((v,i)=>{if(!Number.isFinite(v)){started=false;return;}path+=(started?'L':'M')+x(i).toFixed(2)+' '+y(v).toFixed(2)+' ';started=true;svg+='<circle cx="'+x(i)+'" cy="'+y(v)+'" r="2.8" fill="'+colors[metric.value]+'"/>';});
    svg+='<path d="'+path+'" fill="none" stroke="'+colors[metric.value]+'" stroke-width="2.2" vector-effect="non-scaling-stroke"/><g id="pe-chart-marker" visibility="hidden"><line id="pe-chart-hover-line" y1="24" y2="312" stroke="#94a3b8" stroke-dasharray="4 4"/><circle id="pe-chart-hover-dot" r="4.5" fill="#e0f2fe"/></g>';chart.innerHTML=svg;
  }
  async function loadEvolution(){
    if(!ready)return;if(chartController)chartController.abort();chartController=new AbortController();const signal=chartController.signal;
    points=[];viewStart=0;viewEnd=0;renderEvolution();window.eveosintEconomicsChartLoaded=true;
    const query=new URLSearchParams({from:document.getElementById('pe-chart-from').value,to:document.getElementById('pe-chart-to').value,basis:basis.value,window:document.getElementById('pe-window').value,reference:document.getElementById('pe-reference').value});
    const monthIndex=m=>Number(m.slice(0,4))*12+Number(m.slice(5,7))-1,totalMonths=monthIndex(query.get('to'))-monthIndex(query.get('from'))+1;
    let offset=0;try{do{const current=monthIndex(query.get('from'))+offset,currentMonth=Math.floor(current/12)+'-'+String(current%12+1).padStart(2,'0');chartStatus.textContent='Loading monthly evolution… '+points.length+' / '+totalMonths+' months · Calculating '+currentMonth; query.set('offset',String(offset));const data=await json(base+'/evolution?'+query,signal);if(signal.aborted)return;points.push(...data.points);offset=data.next_offset;viewEnd=points.length-1;renderEvolution();}while(offset!==null);chartStatus.textContent=points.length+' months · '+basis.selectedOptions[0].textContent+' · '+unitCaption();}catch(e){if(e.name!=='AbortError'){chartStatus.textContent='Loaded '+points.length+' / '+totalMonths+' months. '+e.message;window.eveosintEconomicsChartLoaded=false;}}
  }
  function nearest(clientX){const r=chart.getBoundingClientRect(),ratio=Math.max(0,Math.min(1,((clientX-r.left)/r.width*1000-74)/898));return Math.round(viewStart+ratio*(viewEnd-viewStart));}
  function reset(){viewStart=0;viewEnd=Math.max(0,points.length-1);renderEvolution();}
  chart.addEventListener('wheel',e=>{e.preventDefault();const span=viewEnd-viewStart+1;if(points.length<3)return;const n=Math.max(2,Math.min(points.length,span+(e.deltaY<0?-Math.max(1,Math.round(span*.2)):Math.max(1,Math.round(span*.25))))),anchor=nearest(e.clientX),ratio=(anchor-viewStart)/Math.max(1,span-1);viewStart=Math.max(0,Math.min(points.length-n,Math.round(anchor-ratio*(n-1))));viewEnd=viewStart+n-1;renderEvolution();},{passive:false});
  chart.addEventListener('pointerdown',e=>{if(e.button!==0)return;drag={x:e.clientX,start:viewStart,span:viewEnd-viewStart+1};chart.setPointerCapture(e.pointerId);chart.classList.add('dragging');hideTip();});
  chart.addEventListener('pointermove',e=>{
    if(drag){const delta=Math.round(-(e.clientX-drag.x)/chart.getBoundingClientRect().width*drag.span);viewStart=Math.max(0,Math.min(points.length-drag.span,drag.start+delta));viewEnd=viewStart+drag.span-1;renderEvolution();return;}
    const idx=nearest(e.clientX),point=points[idx],range=bounds();if(!point || point.values[metric.value]==null || !range){hideTip();return;}
    const rect=chart.getBoundingClientRect(),x=viewStart===viewEnd?523:74+(idx-viewStart)/(viewEnd-viewStart)*898,y=24+(range[1]-Number(point.values[metric.value]))/(range[1]-range[0])*288;
    const marker=document.getElementById('pe-chart-marker');if(marker){marker.setAttribute('visibility','visible');const line=document.getElementById('pe-chart-hover-line');line.setAttribute('x1',x);line.setAttribute('x2',x);const dot=document.getElementById('pe-chart-hover-dot');dot.setAttribute('cx',x);dot.setAttribute('cy',y);}
    tip.textContent=point.month+' · '+metric.selectedOptions[0].textContent+' · '+full(point.values[metric.value])+' '+unitCaption()+(point.denominator===null || metric.value.endsWith('_index')?'':' · denominator: '+full(point.denominator));tip.hidden=false;tip.style.left=Math.max(8,Math.min(rect.width-tip.offsetWidth-8,e.clientX-rect.left+14))+'px';tip.style.top=Math.max(8,e.clientY-rect.top-64)+'px';
  });
  function endDrag(){drag=null;chart.classList.remove('dragging');}
  chart.addEventListener('pointerup',endDrag);chart.addEventListener('pointercancel',endDrag);chart.addEventListener('lostpointercapture',endDrag);chart.addEventListener('pointerleave',hideTip);chart.addEventListener('dblclick',reset);
  document.getElementById('pe-chart-reset').addEventListener('click',reset);document.getElementById('pe-chart-load').addEventListener('click',loadEvolution);
  metric.addEventListener('change',()=>{const index=metric.value.endsWith('_index');basis.disabled=index;if(index){basis.value='total';loadEvolution();}else{renderEvolution();if(points.length)chartStatus.textContent=points.length+' months · '+basis.selectedOptions[0].textContent+' · '+unitCaption();}});basis.addEventListener('change',loadEvolution);
  document.getElementById('pe-chart-csv').addEventListener('click',()=>{if(!points.length)return;const csv='month,'+metric.value+'_'+basis.value+',denominator,ppa_reference_month\n'+points.slice(viewStart,viewEnd+1).map(p=>p.month+','+(p.values[metric.value]??'')+','+(p.denominator??'')+','+document.getElementById('pe-reference').value).join('\n')+'\n';const url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download='{{ profile.entity_type }}_{{ profile.entity_id | int }}_economics.csv';a.click();URL.revokeObjectURL(url);});
