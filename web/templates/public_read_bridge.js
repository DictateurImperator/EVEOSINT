(() => {
 const source = {{ public_target.read_scope_url|tojson }};
 const policy = {{ public_policy|tojson }};
 window.eveosintPublicError ||= (error) => error?.message || 'This data could not be loaded.';
 const nativeFetch = window.fetch.bind(window);
 window.fetch = (input, options={}) => {
  const url=new URL(input instanceof Request?input.url:String(input),location.origin);
  const method=(options.method || (typeof input==='object'?input.method:'GET') || 'GET').toUpperCase();
  if(url.origin!==location.origin) return Promise.reject(new Error('External requests are unavailable.'));
  if(url.pathname.startsWith('/public/') || url.pathname.startsWith('/static/')) return nativeFetch(input,options);
  if(url.pathname==='/api/update-status' && {{ (not public_site)|tojson }}) return nativeFetch(input,options);
  if(method!=='GET') {
   if(method!=='POST')return Promise.reject(new Error('This action requires login.'));
   const payload=typeof options.body==='string'?JSON.parse(options.body):null;
   return nativeFetch('/public/compute',{...options,method:'POST',credentials:'omit',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({source,path:url.pathname,payload})});
  }
  const params=new URLSearchParams({source,path:url.pathname+url.search});
  const forwarded={...options,method:'GET',cache:'default',credentials:'omit'};
  delete forwarded.body;
  return nativeFetch('/public/read?'+params.toString(),forwarded);
 };
 const nativeOpen=XMLHttpRequest.prototype.open;
 XMLHttpRequest.prototype.open=function(method,input,...rest) {
  const url=new URL(String(input),location.origin);
  if(url.origin!==location.origin) throw new Error('External requests are unavailable.');
  if(!(url.pathname.startsWith('/public/') || url.pathname.startsWith('/static/'))) {
   if(String(method).toUpperCase()!=='GET') throw new Error('This action requires login.');
   input='/public/read?'+new URLSearchParams({source,path:url.pathname+url.search});
  }
  return nativeOpen.call(this,method,input,...rest);
 };
 function publicLink(anchor) {
  const raw=anchor.getAttribute('href'); if(!raw || raw.startsWith('#')) return;
  const url=new URL(raw,location.origin); if(url.origin!==location.origin) return;
  let path=url.pathname.replace(/^\/public(?=\/)/,'');
  let match=path.match(/^\/(character|corporation|alliance|coalition|system|constellation|region|ship|kill|weapon|skill|commodity)\/[1-9]\d*$/);
  let nested=path.match(/^\/(corporation|alliance|coalition)\/[1-9]\d*\/pilotable-ships\/[1-9]\d*$/);
  let extra={{ extra_public_pages|default({})|tojson }};
  let mapPath=path.match(/^\/map(?:\/(region|constellation|system)\/[1-9]\d*)?$/);
  let key=extra[path]?extra[path][0]:mapPath?'map':match?({weapon:'modules',skill:'skills',commodity:'commodities'}[match[1]]||match[1]):nested?nested[1]:path.startsWith('/kill/mer/')?'kill':path==='/leaderboard'?'leaderboard':path.startsWith('/superintel')?'superintel':path==='/killboard'?'killboard':null;
  let tab=extra[path]?extra[path][1]:mapPath?(mapPath[1]||'universe'):key==='leaderboard'?(url.searchParams.get('kind')||'character'):key==='superintel'?(path==='/superintel'?'dashboard':path.split('/').pop()):key==='kill'?'detail':key==='killboard'?'killboard':nested?'pilotable-ships':url.searchParams.get('stats_view')==='top-kills'?'top-kills':url.searchParams.get('tab')||(key==='coalition'?'killboard':'overview');
  if(key && tab==='overview' && !policy[key]?.tabs.includes(tab) && policy[key]?.enabled && policy[key].tabs.includes('killboard')){url.searchParams.set('tab','killboard');tab='killboard';}
  if(key && tab==='overview' && policy[key]?.enabled && !policy[key].tabs.includes(tab) && policy[key].tabs.length){tab=policy[key].tabs[0];url.searchParams.set('tab',tab);}
  if(key==='coalition' && tab==='killboard' && !url.searchParams.has('tab') && policy[key]?.enabled && !policy[key].tabs.includes(tab) && policy[key].tabs.length){tab=policy[key].tabs[0];url.searchParams.set('tab',tab);}
  if(key && policy[key]?.enabled && (policy[key].tabs.includes(tab) || (tab==='index' && policy[key].tabs.length))) anchor.href='/public'+path+url.search+url.hash;
  else { const span=document.createElement('span');span.className=anchor.className;span.replaceChildren(...anchor.childNodes);anchor.replaceWith(span); }
 }
 function links(root) { if(root.nodeType!==1)return;if(root.matches('a[href]'))publicLink(root);root.querySelectorAll('a[href]').forEach(publicLink); }
 const start=()=>{const root=document.getElementById('publishedContent');if(!root)return;links(root);new MutationObserver(records=>records.forEach(r=>r.addedNodes.forEach(links))).observe(root,{childList:true,subtree:true});};
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start);else start();
})();
