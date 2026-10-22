(()=>{
  const $=id=>document.getElementById(id),kind=$('rankingKind');
  const year=$('rankYear'),month=$('rankMonth');
  const availableMonths=[...month.options].map(option=>({value:option.value,label:option.textContent,year:option.dataset.year}));
  function showMonths(preferred){
    const options=availableMonths.filter(option=>option.year===year.value);
    month.replaceChildren(...options.map(option=>new Option(option.label,option.value)));
    if(options.some(option=>option.value===preferred))month.value=preferred;
  }
  const selectedMonth=month.value;showMonths(selectedMonth);
  year.addEventListener('change',()=>{
    const preferred=year.value+'-'+month.value.slice(5);showMonths(preferred);
    month.dispatchEvent(new Event('change'));
  });
  document.querySelectorAll('[data-rank-kind]').forEach(tab=>tab.addEventListener('click',()=>{
    const params=new URLSearchParams(new FormData(kind.form));
    for(const name of ['q','target_id','position','page'])params.delete(name);
    params.set('kind',tab.dataset.rankKind);tab.href=(location.pathname.startsWith('/public/')?'/public':'')+'/leaderboard?'+params.toString();
  }));
  const typeLabel=kind=>({universe:'Universe',coalition:'Coalition',alliance:'Alliance',corporation:'Corporation',character:'Character'}[kind]||kind);
  function logo(item){
    if(item.entity_type==='universe'||Number(item.entity_id)===0)return null;
    if(item.logo_url)return item.logo_url;
    if(['character','corporation','alliance'].includes(item.entity_type))return `https://images.evetech.net/${item.entity_type==='character'?'characters':item.entity_type==='corporation'?'corporations':'alliances'}/${item.entity_id}/${item.entity_type==='character'?'portrait':'logo'}?size=64`;
    return null;
  }
  function icon(item){const url=logo(item);if(url){const img=document.createElement('img');img.src=url;img.alt='';img.loading='lazy';return img;}const span=document.createElement('span');span.className='rank-filter-placeholder';span.textContent=item.entity_type==='universe'?'∞':Number(item.entity_id)===0?'∅':typeLabel(item.entity_type)[0];return span;}
  function autocomplete(inputId,resultsId,purpose,onPick){
    const input=$(inputId),results=$(resultsId);if(!input)return;
    let timer,sequence=0,controller;
    function clear(){results.replaceChildren();results.classList.remove('show');input.setAttribute('aria-expanded','false');}
    function cancel(){clearTimeout(timer);++sequence;controller?.abort();clear();}
    function message(text){clear();const note=document.createElement('div');note.className='kb-empty p-2';note.textContent=text;results.append(note);results.classList.add('show');input.setAttribute('aria-expanded','true');}
    input.setAttribute('aria-controls',resultsId);input.setAttribute('aria-expanded','false');
    input.addEventListener('input',()=>{
      cancel();const current=sequence;if(input.value.trim().length<2)return;
      message('Searching…');
      timer=setTimeout(async()=>{
        controller=new AbortController();
        try{
          const url=new URL('/leaderboard/lookup',location.origin);url.search=new URLSearchParams({kind:kind.value,purpose,q:input.value.trim(),through:month.value||month.dataset.defaultThrough||''});
          const response=await fetch(url,{signal:controller.signal});
          if(current!==sequence)return;
          if(!response.ok){message('Search unavailable. Try again or type more letters.');return;}
          const data=await response.json();if(current!==sequence)return;clear();
          for(const item of data.results||[]){
            const button=document.createElement('button');button.type='button';button.className='kb-search-result';button.append(icon(item));
            const text=document.createElement('span');text.className='kb-search-result-main';
            const name=document.createElement('span');name.className='kb-search-result-name d-block';name.textContent=(item.name||item.label)+(item.ticker?` [${item.ticker}]`:'');
            const meta=document.createElement('span');meta.className='kb-search-result-meta d-block';meta.textContent=[typeLabel(item.entity_type),item.entity_type==='character'?item.corporation_name:null,item.alliance_name].filter(Boolean).join(' · ');
            text.append(name,meta);button.append(text);button.onclick=()=>{cancel();onPick(item);};results.append(button);
          }
          if(!results.childElementCount){message('No matching entities.');return;}
          results.classList.add('show');input.setAttribute('aria-expanded','true');
        }catch(error){if(current===sequence&&error.name!=='AbortError')message('Search unavailable. Try again.');}
      },250);
    });
    input.addEventListener('keydown',event=>{
      if(event.key==='Escape')cancel();
      const first=results.querySelector('button');
      if(first&&event.key==='ArrowDown'){event.preventDefault();first.focus();}
      if(first&&event.key==='Enter'){event.preventDefault();first.click();}
    });
    results.addEventListener('keydown',event=>{
      const buttons=[...results.querySelectorAll('button')],index=buttons.indexOf(document.activeElement);
      if(event.key==='Escape'){cancel();input.focus();}
      if(index>=0&&['ArrowDown','ArrowUp'].includes(event.key)){event.preventDefault();buttons[(index+(event.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length].focus();}
    });
    kind.addEventListener('change',cancel);
    month.addEventListener('change',cancel);
    document.addEventListener('click',event=>{if(!input.parentElement.contains(event.target))cancel();});
  }
  autocomplete('rankName','rankNameResults','target',item=>{$('rankName').value=item.name;$('rankTarget').value=item.entity_id;$('rankPosition').value='';});
  $('rankName').addEventListener('input',()=>{$('rankTarget').value='';$('rankPosition').value='';});
  $('rankPosition').addEventListener('input',()=>{$('rankName').value='';$('rankTarget').value='';});
  kind.addEventListener('change',()=>{$('rankTarget').value='';});
  const container=$('rankScopeChips');
  function summary(){const positive=container.querySelectorAll('input[name="include"]').length,negative=container.querySelectorAll('input[name="exclude"]').length;$('rankFilterSummary').textContent=positive||negative?`+ ${positive} / − ${negative}`:'ALL';}
  function bindTerm(term){
    const input=term.querySelector('input'),button=term.querySelector('[data-rank-sign]');
    function update(){const exclude=input.name==='exclude';term.classList.toggle('kb-term-minus',exclude);button.textContent=exclude?'−':'+';button.classList.toggle('btn-outline-danger',exclude);button.classList.toggle('btn-outline-primary',!exclude);button.setAttribute('aria-label',exclude?'Excluded: click to include':'Included: click to exclude');summary();}
    button.onclick=()=>{input.name=input.name==='exclude'?'include':'exclude';update();};
    term.querySelector('[data-rank-remove]').onclick=()=>{term.remove();summary();};update();
  }
  container.querySelectorAll('[data-rank-term]').forEach(bindTerm);summary();
  $('rankFilterToggle').onclick=()=>{const open=$('rankFilter').classList.toggle('open');$('rankFilterToggle').setAttribute('aria-expanded',String(open));};
  $('rankFilterClear').onclick=()=>{container.replaceChildren();summary();};
  function addScope(item){
    const value=`${item.entity_type}:${item.entity_id}`;
    const existing=[...container.querySelectorAll('input')].find(input=>input.value===value);
    if(existing){existing.parentElement.querySelector('[data-rank-sign]').focus();$('rankScopeName').value='';return;}
    const term=document.createElement('div');term.className='kb-term';term.dataset.rankTerm='';
    const sign=document.createElement('button');sign.type='button';sign.className='btn btn-sm kb-term-sign btn-outline-primary';sign.dataset.rankSign='';
    const text=document.createElement('div');text.className='kb-term-name';const name=document.createElement('div');name.className='kb-term-name-main';name.textContent=item.name+(item.ticker?` [${item.ticker}]`:'');
    const meta=document.createElement('div');meta.className='kb-term-name-meta';meta.textContent=typeLabel(item.entity_type);text.append(name,meta);
    const input=document.createElement('input');input.type='hidden';input.name='include';input.value=value;
    const remove=document.createElement('button');remove.type='button';remove.className='kb-term-remove';remove.dataset.rankRemove='';remove.textContent='×';remove.setAttribute('aria-label','Remove filter');
    term.append(sign,icon(item),text,input,remove);container.append(term);bindTerm(term);$('rankScopeName').value='';
  }
  autocomplete('rankScopeName','rankScopeResults','scope',addScope);
  $('rankAddUniverse').onclick=()=>addScope({entity_type:'universe',entity_id:0,name:'Universe'});
  $('rankAddNoAlliance').onclick=()=>addScope({entity_type:'alliance',entity_id:0,name:'No alliance'});
  function updateSpecialButtons(){$('rankAddNoAlliance').hidden=kind.value==='alliance';}
  kind.addEventListener('change',updateSpecialButtons);updateSpecialButtons();
})();
