"""The gallery page (/gallery): every saved press from ~/.snip2tex/history, with
the selection, what came back and what was said. Opened from the dashboard's
Gallery button or the menu bar, never shown on its own."""

PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Snip2TeX Gallery</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#fafaf9;--fg:#1c1c1c;--mut:#777;--card:#fff;--line:#e5e5e5;--bad:#c0392b;--sel:#1c1c1c;--selfg:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#161616;--fg:#eee;--mut:#999;--card:#202020;--line:#333;--sel:#eee;--selfg:#161616}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px -apple-system,system-ui,sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
header h1{font-size:18px;margin:0 8px 0 0}
a.back{color:var(--mut);text-decoration:none;margin-right:4px}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:999px;padding:4px 11px;cursor:pointer;font:inherit}
.chip.on{background:var(--sel);color:var(--selfg);border-color:var(--sel)}
input[type=search]{flex:1;min-width:160px;max-width:320px;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:6px 10px;font:inherit}
.count{color:var(--mut);font-size:12px;margin-left:auto}
main{max-width:1200px;margin:0 auto;padding:8px 16px 40px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin:22px 0 10px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px}
.item{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;cursor:pointer;display:flex;flex-direction:column}
.item:hover{border-color:var(--mut)}
.thumb{height:150px;background:#fff;display:flex;align-items:center;justify-content:center;border-bottom:1px solid var(--line)}
.thumb img{max-width:100%;max-height:100%;object-fit:contain}
.thumb .none{color:#999;font-size:12px}
.cap{padding:8px 10px;display:flex;flex-direction:column;gap:3px;min-height:58px}
.cap .top{display:flex;gap:6px;align-items:center;font-size:12px;color:var(--mut)}
.badge{font-weight:600;color:var(--fg)}
.cap .txt{font-size:13px;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.err{color:var(--bad)}
.more{display:block;margin:20px auto;padding:8px 16px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer;font:inherit}
.empty{color:var(--mut);padding:40px 0;text-align:center}
#detail{position:fixed;inset:0;background:rgba(0,0,0,.45);display:none;z-index:5;padding:24px;overflow:auto}
#detail .box{background:var(--card);border-radius:12px;max-width:1100px;margin:0 auto;padding:18px;position:relative}
#detail .close{position:absolute;top:10px;right:12px;border:none;background:none;color:var(--fg);font-size:22px;cursor:pointer}
.pics{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:12px 0}
@media(max-width:700px){.pics{grid-template-columns:1fr}}
.pic{border:1px solid var(--line);border-radius:8px;background:#fff;padding:8px;text-align:center}
.pic img{max-width:100%;max-height:60vh}
.pic .lbl{font-size:11px;color:#777;text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px}
.field{margin:10px 0}
.field .lbl{font-size:12px;color:var(--mut);margin-bottom:3px;display:flex;gap:8px;align-items:center}
.field pre,.field .val{white-space:pre-wrap;margin:0;background:var(--bg);padding:8px 10px;border-radius:6px;font-size:13px;max-height:260px;overflow:auto}
.lbl button,.actions button{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:6px;padding:2px 8px;cursor:pointer;font:inherit;font-size:12px}
.actions{display:flex;gap:8px;margin-top:14px}
.actions button{padding:6px 12px;font-size:13px}
.actions .del{color:var(--bad)}
.metaline{color:var(--mut);font-size:12px}
</style></head><body>
<header>
 <a class="back" href="/">‹ Dashboard</a><h1>Gallery</h1>
 <div class="chips" id="chips"></div>
 <input type="search" id="q" placeholder="Search text, LaTeX, model…">
 <span class="count" id="count"></span>
</header>
<main id="list"><div class="empty">Loading…</div></main>
<div id="detail" onclick="if(event.target===this)closeDetail()"><div class="box" id="box"></div></div>
<script>
const BUTTONS={all:'All',snip:'∑',eval:'=',graph:'Graph',ask:'?',solve:'Solve',check:'Check'};
const NAMES={older:'Earlier',snip:'∑ LaTeX',eval:'= evaluate',graph:'Graph',ask:'? ask',solve:'Solve steps',check:'Check work'};
let items=[],filter='all',shown=120;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const file=(it,f)=>`/history/${it.dir.split('/').map(encodeURIComponent).join('/')}/${f}`;
function day(t){const d=new Date(t*1000),n=new Date();const y=new Date(n);y.setDate(n.getDate()-1);
 if(d.toDateString()===n.toDateString())return 'Today';if(d.toDateString()===y.toDateString())return 'Yesterday';
 return d.toLocaleDateString(undefined,{weekday:'short',year:'numeric',month:'short',day:'numeric'});}
const clock=t=>new Date(t*1000).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});
function text(it){return it.error?it.error:(it.result||it.latex||it.render_error||'');}
function matches(it){
 if(filter!=='all'&&it.button!==filter)return false;
 const q=document.getElementById('q').value.trim().toLowerCase();
 if(!q)return true;
 return [it.result,it.latex,it.tikz,it.model,it.kind,it.error,NAMES[it.button]].some(v=>v&&String(v).toLowerCase().includes(q));}
function chips(){document.getElementById('chips').innerHTML=Object.entries(BUTTONS).map(([k,v])=>
 `<button class="chip${filter===k?' on':''}" onclick="setFilter('${k}')">${esc(v)}</button>`).join('');}
function setFilter(k){filter=k;shown=120;chips();render();}
function render(){
 const list=items.filter(matches), el=document.getElementById('list');
 document.getElementById('count').textContent=`${list.length} of ${items.length}`;
 if(!list.length){el.innerHTML=`<div class="empty">${items.length?'Nothing matches.':'Nothing saved yet. Every button press on the tablet shows up here.'}</div>`;return;}
 let html='',cur=null;
 list.slice(0,shown).forEach(it=>{
  const d=day(it.time);if(d!==cur){if(cur)html+='</div>';html+=`<h2>${esc(d)}</h2><div class="grid">`;cur=d;}
  const pic=it.has_output?file(it,'output.png'):it.has_input?file(it,'input.png'):'';
  const t=text(it);
  html+=`<div class="item" onclick="openDetail('${esc(it.dir)}')"><div class="thumb">${pic?`<img loading="lazy" src="${pic}">`:'<span class="none">no image</span>'}</div>
   <div class="cap"><div class="top"><span class="badge">${esc(NAMES[it.button]||it.button||'')}</span><span>${clock(it.time)}</span>${it.kind&&it.kind!==it.button?`<span>· ${esc(it.kind)}</span>`:''}</div>
   ${t?`<div class="txt${it.error?' err':''}">${esc(t)}</div>`:''}</div></div>`;});
 html+='</div>';
 if(list.length>shown)html+=`<button class="more" onclick="shown+=120;render()">Show more (${list.length-shown} left)</button>`;
 el.innerHTML=html;}
function field(label,val,pre){if(!val)return '';const id='f'+Math.random().toString(36).slice(2);
 return `<div class="field"><div class="lbl">${esc(label)} <button onclick="copy('${id}')">Copy</button></div>${pre?`<pre id="${id}">${esc(val)}</pre>`:`<div class="val" id="${id}">${esc(val)}</div>`}</div>`;}
function openDetail(dir){const it=items.find(i=>i.dir===dir);if(!it)return;
 const pics=(it.has_input?`<div class="pic"><div class="lbl">Selection</div><img src="${file(it,'input.png')}"></div>`:'')+
  (it.has_output?`<div class="pic"><div class="lbl">Sent back</div><img src="${file(it,'output.png')}"></div>`:'');
 document.getElementById('box').innerHTML=`<button class="close" onclick="closeDetail()">×</button>
  <div style="font-size:16px;font-weight:600">${esc(NAMES[it.button]||it.button||'Snip')}${it.kind&&it.kind!==it.button?` · ${esc(it.kind)}`:''}</div>
  <div class="metaline">${esc(new Date(it.time*1000).toLocaleString())}${it.model?' · '+esc(it.model):''}${it.seconds!=null?' · '+it.seconds+' s':''}${it.status&&it.status!=='ok'?' · <span class="err">'+esc(it.status)+'</span>':''}</div>
  ${pics?`<div class="pics">${pics}</div>`:''}
  ${field(it.button==='ask'||it.button==='solve'?'Answer / caption':'Result',it.result)}
  ${field('LaTeX',it.latex,true)}${field('TikZ',it.tikz,true)}
  ${it.graph?field('Graph spec',JSON.stringify(it.graph,null,2),true):''}
  ${field('Error',it.error||it.render_error,true)}
  <div class="actions"><button onclick="reveal('${esc(it.dir)}')">Show in Finder</button><button class="del" onclick="del('${esc(it.dir)}')">Delete</button></div>`;
 document.getElementById('detail').style.display='block';
 history.replaceState(null,'','#'+encodeURIComponent(dir));}
function closeDetail(){document.getElementById('detail').style.display='none';history.replaceState(null,'',location.pathname);}
function copy(id){navigator.clipboard.writeText(document.getElementById(id).textContent);}
async function reveal(dir){await fetch('/api/history/reveal',{method:'POST',body:JSON.stringify({dir})});}
async function del(dir){if(!confirm('Delete this from the gallery? Its folder is removed from ~/.snip2tex/history.'))return;
 const r=await fetch('/api/history/delete',{method:'POST',body:JSON.stringify({dir})});
 if(r.ok){items=items.filter(i=>i.dir!==dir);closeDetail();render();}}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeDetail();});
document.getElementById('q').addEventListener('input',()=>{shown=120;render();});
async function load(){items=await (await fetch('/api/history')).json();chips();render();
 if(location.hash.length>1)openDetail(decodeURIComponent(location.hash.slice(1)));}
load();
</script></body></html>"""
