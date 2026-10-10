  const bestStatus=document.getElementById('pe-best-status'),bestResult=document.getElementById('pe-best-result');
  const bestUse=document.getElementById('pe-best-use'),bestStop=document.getElementById('pe-best-stop');
  let bestController=null,bestMonth=null,ranked=[],rankingReference='';
  function renderRanking(){const body=document.getElementById('pe-best-ranking');body.replaceChildren();const limit=Number(document.getElementById('pe-best-limit').value);ranked.slice(0,limit).forEach((entry,index)=>{const tr=document.createElement('tr');cell(tr,index+1);const monthCell=document.createElement('td'),choose=document.createElement('button');choose.type='button';choose.className='btn btn-sm btn-link p-0';choose.textContent=entry.month;choose.title='Use as observed month';choose.addEventListener('click',()=>{to.value=entry.month;calculate();});monthCell.appendChild(choose);tr.appendChild(monthCell);const amount=document.createElement('td');amount.textContent=compact(entry.value)+' PPA ISK';amount.title=full(entry.value)+' PPA ISK (reference '+rankingReference+')';tr.appendChild(amount);body.appendChild(tr);});}
  document.getElementById('pe-best-limit').addEventListener('change',renderRanking);
  function compareExact(a,b){
    function parts(value){const m=String(value).match(/^([+-]?)(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/);if(!m)throw new Error('Invalid economic value.');return [BigInt((m[1]==='-'?'-':'')+m[2]+(m[3]||'')),(m[3]||'').length-Number(m[4]||0)];}
    const [aa,as]=parts(a),[bb,bs]=parts(b),scale=Math.max(as,bs);const left=aa*10n**BigInt(scale-as),right=bb*10n**BigInt(scale-bs);return left>right?1:left<right?-1:0;
  }
  function invalidateBest(){if(bestController)bestController.abort();bestMonth=null;ranked=[];renderRanking();bestResult.replaceChildren();bestStatus.textContent='';bestUse.hidden=true;bestStop.hidden=true;}
  for(const id of ['pe-reference','pe-window','pe-best-metric','pe-best-basis'])document.getElementById(id).addEventListener('change',invalidateBest);
  bestStop.addEventListener('click',()=>{if(bestController)bestController.abort();bestStop.hidden=true;bestUse.hidden=true;bestStatus.textContent+=' · Search stopped; displayed result is provisional.';});
  document.getElementById('pe-best-find').addEventListener('click',async()=>{
    if(!ready)return;invalidateBest();bestController=new AbortController();const signal=bestController.signal;
    const reference=document.getElementById('pe-reference').value;
    if(!reference){bestStatus.textContent='A CPI reference month is required.';return;}
    rankingReference=reference;
    const selectedMetric=document.getElementById('pe-best-metric'),selectedBasis=document.getElementById('pe-best-basis');
    const label=selectedMetric.selectedOptions[0].textContent+' · '+selectedBasis.selectedOptions[0].textContent;
    const params=new URLSearchParams({metric:selectedMetric.value,basis:selectedBasis.value,reference,window:document.getElementById('pe-window').value});
    let offset=0,checked=0,available=0,total=0,best=null;bestStop.hidden=false;
    try{do{params.set('offset',String(offset));bestStatus.textContent='Searching all MER history… '+checked+(total?' / '+total:'')+' months';const d=await json(base+'/best-month?'+params,signal);if(signal.aborted)return;
      checked+=d.checked;available+=d.available;total=d.total_months;offset=d.next_offset;
      ranked.push(...(d.ranked_months||[]));ranked.sort((a,b)=>compareExact(b.value,a.value)||a.month.localeCompare(b.month));ranked=ranked.slice(0,20);renderRanking();
      if(d.best && (!best || compareExact(d.best.value,best.value)>0 || (compareExact(d.best.value,best.value)===0 && d.best.month<best.month))){best=d.best;bestMonth=best.month;bestResult.replaceChildren();const value=document.createElement('strong');value.textContent=best.month+' · '+compact(best.value)+' PPA ISK';value.title=full(best.value)+' PPA ISK (reference '+reference+')';bestResult.append(value);}
    }while(offset!==null);
      bestStatus.textContent=label+' · Reference '+reference+' · '+checked+' months checked; '+available+' usable; '+(checked-available)+' excluded.';
      if(!best)bestResult.textContent='No PPA values available for this indicator.';bestUse.hidden=!best;
    }catch(e){if(e.name!=='AbortError'){bestStatus.textContent='Search incomplete: '+e.message+' Any displayed best month is provisional.';bestUse.hidden=true;}}finally{if(!signal.aborted)bestStop.hidden=true;}
  });
  bestUse.addEventListener('click',()=>{if(!bestMonth)return;to.value=bestMonth;calculate();});
