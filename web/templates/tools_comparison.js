(() => {
  const root=document.getElementById('comparison-tool'), economic=root.dataset.kind==='economics';
  const el=id=>document.getElementById('tc-'+id), colors=['#38bdf8','#fb923c','#a78bfa','#4ade80','#f472b6','#facc15'];
  const entities=[], series=[];let aborter=null, searchAborter=null, searchTimer, searchVersion=0, dates=[], start=0,end=0,drag=null,runVersion=0;
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const full=v=>Number(v).toLocaleString('en-US',{maximumFractionDigits:6});
  const compact=v=>{const n=Number(v);for(const [d,s] of [[1e12,' T'],[1e9,' B'],[1e6,' M']])if(Math.abs(n)>=d)return (n/d).toLocaleString('en-US',{maximumFractionDigits:2})+s;return n.toLocaleString('en-US',{maximumFractionDigits:2});};
  async function json(url,signal){const r=await fetch(url,{signal,headers:{Accept:'application/json'}});if(!r.ok)throw Error('Request failed ('+r.status+').');const data=await r.json();if(data.error)throw Error(data.error);return data;}
  function cancel(){runVersion++;if(aborter)aborter.abort();el('run').disabled=false;el('stop').disabled=true;}
  function invalidate(){cancel();series.length=0;dates=[];el('status').textContent='Selection changed. Click Compare to calculate.';render();}
  function selected(){el('selected').innerHTML=entities.map((e,i)=>'<button class="btn btn-outline-info btn-sm" data-remove="'+i+'">'+esc(e.name)+' · '+esc(e.kind)+' ×</button>').join('');el('selected').querySelectorAll('button').forEach(b=>b.onclick=()=>{entities.splice(Number(b.dataset.remove),1);selected();invalidate();});}
  async function search(){
    const q=el('search').value.trim(),version=++searchVersion;if(searchAborter)searchAborter.abort();el('results').replaceChildren();if(!q)return;
    searchAborter=new AbortController();try{
      const data=await json('/api/tools/entity-search?q='+encodeURIComponent(q),searchAborter.signal);
      if(version!==searchVersion)return;
      const found=(data.results||[]).filter(e=>['alliance','coalition'].includes(e.entity_type));
      el('results').innerHTML=found.map((e,i)=>'<button class="list-group-item list-group-item-action" data-result="'+i+'">'+esc(e.name||e.label)+' · '+esc(e.entity_type)+'</button>').join('')||'<div class="muted-line p-2">No alliances or coalitions found.</div>';
      el('results').querySelectorAll('button').forEach(b=>b.onclick=async()=>{
        const e=found[Number(b.dataset.result)],id=Number(e.entity_id);
        if(entities.length>=6){el('status').textContent='Select up to six entities.';return;}
        if(!Number.isSafeInteger(id)||id<=0||entities.some(x=>x.kind===e.entity_type&&x.id===id))return;
        entities.push({kind:e.entity_type,id,name:e.name||e.label});selected();invalidate();el('search').value='';el('results').replaceChildren();
        if(economic&&!el('from').value){try{const options=await json('/api/'+e.entity_type+'/'+id+'/population-economics/options');if(!el('from').value&&options.months.length){el('to').value=options.months.at(-1);el('from').value=options.months[Math.max(0,options.months.length-12)];el('reference').value=options.months.at(-1);}}catch(err){el('status').textContent=err.message;}}
      });
    }catch(err){if(err.name!=='AbortError')el('status').textContent=err.message;}
  }
  el('search').oninput=()=>{++searchVersion;if(searchAborter)searchAborter.abort();el('results').replaceChildren();clearTimeout(searchTimer);searchTimer=setTimeout(search,250);};
  function bounds(){const values=series.flatMap(s=>dates.slice(start,end+1).map(d=>s.points.get(d))).filter(v=>v!=null&&Number.isFinite(Number(v))).map(Number);if(!values.length)return null;let lo=Math.min(...values),hi=Math.max(...values),pad=hi===lo?Math.max(1,Math.abs(lo)*.05):(hi-lo)*.08;return [lo-pad,hi+pad];}
  function render(){
    const range=bounds();el('csv').disabled=!series.some(s=>s.points.size);el('tip').hidden=true;
    let svg='<svg viewBox="0 0 1000 360" width="100%" role="img" aria-label="Comparison graph">';
    if(!range)svg+='<text x="500" y="180" text-anchor="middle" fill="#94a3b8">No values loaded for this selection.</text>';
    else{
      const [lo,hi]=range,x=i=>start===end?525:80+(i-start)/Math.max(1,end-start)*890,y=v=>25+(hi-Number(v))/(hi-lo)*275;
      for(let t=0;t<5;t++){const py=25+t*275/4;svg+='<line x1="80" x2="970" y1="'+py+'" y2="'+py+'" stroke="#334155"/><text x="72" y="'+(py+4)+'" text-anchor="end" fill="#94a3b8" font-size="12">'+esc(compact(hi-t*(hi-lo)/4))+'</text>';}
      for(let i=start;i<=end;i++){if(i===start||i===end||i%Math.max(1,Math.ceil((end-start)/6))===0)svg+='<text x="'+x(i)+'" y="322" text-anchor="middle" fill="#94a3b8" font-size="11">'+esc(dates[i])+'</text>';}
      series.forEach((s,j)=>{let path='',connected=false;for(let i=start;i<=end;i++){const v=s.points.get(dates[i]);if(v==null||!Number.isFinite(Number(v))){connected=false;continue;}path+=(connected?'L':'M')+x(i)+' '+y(v)+' ';connected=true;svg+='<circle cx="'+x(i)+'" cy="'+y(v)+'" r="2.2" fill="'+colors[j]+'"/>';}if(path)svg+='<path d="'+path+'" fill="none" stroke="'+colors[j]+'" stroke-width="2"/>';});
    }
    el('chart').innerHTML=svg+'</svg><div class="d-flex flex-wrap gap-3 small">'+series.map((s,i)=>'<span style="color:'+colors[i]+'">● '+esc(s.name)+'</span>').join('')+'</div>';
    el('summary').innerHTML=series.map((s,i)=>{const rows=[...s.points].filter(([,v])=>v!=null&&Number.isFinite(Number(v))).sort((a,b)=>a[0].localeCompare(b[0]));if(!rows.length)return '<tr><td>'+esc(s.name)+'</td><td colspan="3">No available data</td></tr>';const a=rows[0],b=rows.at(-1),delta=Number(b[1])-Number(a[1]),pct=Number(a[1])===0?'': ' ('+(delta/Math.abs(Number(a[1]))*100).toFixed(2)+'%)';return '<tr><td style="color:'+colors[i]+'">'+esc(s.name)+'</td><td title="'+esc(full(a[1]))+'">'+esc(a[0])+' · '+compact(a[1])+'</td><td title="'+esc(full(b[1]))+'">'+esc(b[0])+' · '+compact(b[1])+'</td><td title="'+esc(full(delta))+'" style="color:'+(delta>=0?'#4ade80':'#f87171')+'">'+(delta>0?'+':'')+compact(delta)+pct+'</td></tr>';}).join('');
  }
  function refresh(){dates=[...new Set(series.flatMap(s=>[...s.points.keys()]))].sort();start=0;end=Math.max(0,dates.length-1);render();}
  async function run(){
    cancel();const version=runVersion,metric=el('metric').value,from=el('from').value,to=el('to').value,windowDays=Number(el('window').value);
    if(!entities.length||!from||!to||from>to||!Number.isInteger(windowDays)||windowDays<1||windowDays>3650){el('status').textContent='Choose entities, a valid period and an activity window from 1 to 3650 days.';return;}
    if(economic&&!el('reference').value){el('status').textContent='Choose a common PPA reference month.';return;}
    const selection=entities.map(e=>({...e}));series.length=0;dates=[];render();aborter=new AbortController();const signal=aborter.signal;el('run').disabled=true;el('stop').disabled=false;let failures=0;
    try{
      for(const entity of selection){
        const item={...entity,points:new Map()};series.push(item);const base='/api/'+entity.kind+'/'+entity.id;
        try{
          if(economic){
            const options=await json(base+'/population-economics/options',signal);
            if(!options.available){el('status').textContent=entity.name+' has no territorial economic data.';refresh();continue;}
            let offset=0;do{
              el('status').textContent='Loading '+entity.name+' · '+item.points.size+' months…';
              const query=new URLSearchParams({from,to,window:windowDays,basis:el('basis').value,reference:el('reference').value,offset});
              const data=await json(base+'/population-economics/evolution?'+query,signal);
              for(const p of data.points)item.points.set(p.month,p.values[metric]);offset=data.next_offset;refresh();
            }while(offset!==null);
          }else{
            const limit=new Date(to+'T00:00:00Z');let cursor=new Date(from+'T00:00:00Z');
            while(cursor<=limit){
              const last=new Date(['member_count','corporation_count','sovereignty_count'].includes(metric)?limit.getTime():Math.min(limit.getTime(),cursor.getTime()+6*86400000)),a=cursor.toISOString().slice(0,10),b=last.toISOString().slice(0,10);
              el('status').textContent='Loading '+entity.name+' · '+a+' through '+b+'…';
              const query=new URLSearchParams({metric,date_from:a,date_to:b,window:windowDays,mode:el('mode').value});
              const data=await json(base+'/population-intelligence/series?'+query,signal);for(const p of data.rows)item.points.set(p.date,p.value);refresh();cursor=new Date(last.getTime()+86400000);
            }
          }
        }catch(err){if(err.name==='AbortError')throw err;failures++;item.error=err.message;refresh();}
      }
      el('status').textContent='Loaded '+series.length+' entities · '+dates.length+' dates.'+(failures?' '+failures+' failed: '+series.filter(s=>s.error).map(s=>s.name+': '+s.error).join('; '):'');
    }catch(err){if(version===runVersion)el('status').textContent=err.name==='AbortError'?'Stopped. Completed values are kept.':err.message;}
    finally{if(version===runVersion){el('run').disabled=false;el('stop').disabled=true;}}
  }
  el('run').onclick=run;el('stop').onclick=()=>{cancel();el('status').textContent='Stopped. Completed values are kept.';};
  root.querySelectorAll('select,input:not(#tc-search)').forEach(input=>input.addEventListener('change',invalidate));
  const nearest=x=>{const r=el('chart').getBoundingClientRect();return Math.max(start,Math.min(end,Math.round(start+Math.max(0,Math.min(1,(x-r.left)/r.width*1000/890-80/890))*(end-start))));};
  el('chart').addEventListener('wheel',e=>{if(dates.length<3)return;e.preventDefault();const n=Math.max(2,Math.min(dates.length,Math.round((end-start+1)*(e.deltaY<0?.8:1.25)))),anchor=nearest(e.clientX);start=Math.max(0,Math.min(dates.length-n,anchor-Math.floor(n/2)));end=start+n-1;render();},{passive:false});
  el('chart').onpointerdown=e=>{if(!dates.length)return;drag={x:e.clientX,start,span:end-start+1};el('chart').setPointerCapture(e.pointerId);};
  el('chart').onpointermove=e=>{if(drag){start=Math.max(0,Math.min(dates.length-drag.span,drag.start-Math.round((e.clientX-drag.x)/el('chart').getBoundingClientRect().width*drag.span)));end=start+drag.span-1;render();return;}if(!dates.length)return;const date=dates[nearest(e.clientX)];el('tip').textContent=date+'\n'+series.map(s=>s.name+': '+(s.points.get(date)==null?'—':full(s.points.get(date)))).join('\n');el('tip').hidden=false;el('tip').style.left=Math.min(e.pageX+12,window.scrollX+window.innerWidth-el('tip').offsetWidth-12)+'px';el('tip').style.top=(e.pageY+12)+'px';};
  el('chart').onpointerup=el('chart').onpointercancel=()=>drag=null;el('chart').onpointerleave=()=>el('tip').hidden=true;
  el('reset').onclick=()=>{start=0;end=Math.max(0,dates.length-1);render();};el('chart').ondblclick=el('reset').onclick;
  el('csv').onclick=()=>{const quote=v=>'"'+String(v??'').replace(/"/g,'""')+'"',rows=[['Date','Indicator','Basis','PPA reference','PvP window','PvP participation',...series.map(s=>s.name+' ('+s.kind+' '+s.id+')')],...dates.slice(start,end+1).map(d=>[d,el('metric').value,economic?el('basis').value:'',economic?el('reference').value:'',el('window').value,economic?'attacker':el('mode').value,...series.map(s=>s.points.get(d)??'')])];const blob=new Blob([rows.map(r=>r.map(quote).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=root.dataset.kind+'-comparison.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
  if(!economic){const now=new Date(),then=new Date(now);then.setUTCMonth(then.getUTCMonth()-3);el('from').value=then.toISOString().slice(0,10);el('to').value=now.toISOString().slice(0,10);}
  render();
})();
