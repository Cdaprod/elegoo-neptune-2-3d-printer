"use strict";
(() => {
 const $ = id => document.getElementById(id);
 const state = {view:"overview",selected:null,loading:false,login:false,interval:null};
 const number = v => Number.isFinite(Number(v)) && v !== null && v !== "" ? Number(v) : null;
 const value = reading => reading?.status === "available" && number(reading.value) !== null ? number(reading.value).toFixed(1)+"°" : "—";
 const text = (id,val) => { const el=$(id); if(el) el.textContent=String(val ?? "—"); };
 async function api(path,options={}) {
   const res=await fetch(path,{credentials:"same-origin",...options,headers:{"Accept":"application/json",...(options.headers||{})}});
   if(res.status===401){$("login").classList.add("visible");state.login=true;throw new Error("Authentication required");}
   if(!res.ok){let body=await res.json().catch(()=>({}));throw new Error(body.detail||"Request failed: "+res.status);}
   return res.json();
 }
 function route(view){if(!["overview","models","observations","controls"].includes(view))return;state.view=view;document.querySelectorAll(".view").forEach(el=>el.classList.toggle("active",el.id==="view-"+view));document.querySelectorAll("[data-go]").forEach(el=>{if(el.tagName==="BUTTON"){if(el.dataset.go===view)el.setAttribute("aria-current","page");else el.removeAttribute("aria-current");}});if(view==="models")loadFiles();}
 document.querySelectorAll("[data-go]").forEach(b=>b.addEventListener("click",()=>route(b.dataset.go)));
 let gesture=null;
 document.querySelector("main").addEventListener("pointerdown",e=>{if(e.pointerType==="touch"&&!e.target.closest("#models,button,a,input"))gesture={x:e.clientX,y:e.clientY};});
 document.querySelector("main").addEventListener("pointerup",e=>{if(!gesture)return;const dx=e.clientX-gesture.x,dy=e.clientY-gesture.y;gesture=null;if(Math.abs(dx)>90&&Math.abs(dx)>Math.abs(dy)*1.6){const views=["overview","models","observations","controls"];route(views[Math.max(0,Math.min(3,views.indexOf(state.view)+(dx<0?1:-1)))]);}});
 function renderStatus(s){
  const online=Boolean(s.connection?.connected)&&!s.stale;
  const stateName=online?"live":s.stale?"stale":"offline";
  $("health").dataset.state=stateName;
  text("health","● "+(online?"Connected":s.stale?"Stale / unavailable":"Offline"));
  text("age",s.data_age_seconds==null?"No observation":Math.round(s.data_age_seconds)+"s old");
  const t=s.telemetry||{};
  text("nozzle",value(t.nozzle_temperature));text("bed",value(t.bed_temperature));
  text("nozzle-large",value(t.nozzle_temperature));text("bed-large",value(t.bed_temperature));
  const job=s.print_job||{};
  text("job-state",job.status||"Unknown");
  text("job-file",job.filename||job.file||job.name||"No verified job filename");
  const percent=number(job.progress_percent??job.progress?.completion??(typeof job.progress==="number"?job.progress:null));
  $("progress").value=percent===null?0:Math.max(0,Math.min(100,percent));
  text("job-meta",percent===null?"Progress not reported":percent.toFixed(1)+"% reported");
  text("printer-state",online?(t.printer_state?.value??job.status??"Connected"):"Unverified");
  text("printer-source",s.printer_backend==="octoprint"?"OctoPrint backend":"MKS backend");
  text("observed",s.observed_at?"Observed "+new Date(s.observed_at).toLocaleTimeString():"No observation");
  text("integrity",online?"Live":s.stale?"Stale":"Offline");
  text("observations-note",s.last_error||"Read-only telemetry · no synthetic samples");
 }
 function renderChart(data){
  const svg=$("chart");svg.replaceChildren();
  const ns="http://www.w3.org/2000/svg";
  const node=(tag,attrs)=>{const el=document.createElementNS(ns,tag);Object.entries(attrs).forEach(([k,v])=>el.setAttribute(k,String(v)));svg.append(el);return el;};
  const samples=(data.history||[]).slice(-180);
  const available=samples.filter(p=>number(p.nozzle_c)!==null||number(p.bed_c)!==null);
  if(available.length<2){const label=node("text",{x:28,y:118,fill:"#abc1d2"});label.textContent="Not enough observed temperature samples";return;}
  const maximum=Math.max(50,...available.flatMap(p=>[number(p.nozzle_c),number(p.bed_c)].filter(v=>v!==null)));const ceiling=Math.ceil(maximum/25)*25;
  for(let i=0;i<=4;i++){let y=15+i*48;node("line",{x1:38,y1:y,x2:690,y2:y,stroke:"#2e455c","stroke-dasharray":"4 5"});const label=node("text",{x:2,y:y+5,fill:"#9eb5c7","font-size":13});label.textContent=Math.round(ceiling*(4-i)/4);}
  for(const [key,color] of [["nozzle_c","#7bd1ff"],["bed_c","#f8b879"]]){
   let points=[];let segments=[];
   samples.forEach((sample,i)=>{const v=number(sample[key]);if(v===null){if(points.length>1)segments.push(points);points=[];return;}points.push((38+i/Math.max(1,samples.length-1)*652).toFixed(1)+","+(207-v/ceiling*192).toFixed(1));});if(points.length>1)segments.push(points);
   segments.forEach(segment=>node("polyline",{points:segment.join(" "),fill:"none",stroke:color,"stroke-width":3,"stroke-linejoin":"round","stroke-linecap":"round"}));
  }
 }
 function card(file,source){
  const button=document.createElement("div");button.className="model";button.tabIndex=0;button.setAttribute("role","button");
  button.setAttribute("aria-label","Select "+(file.filename||file.name));
  const img=file.preview_available?document.createElement("img"):document.createElement("div");
  if(file.preview_available){
   img.loading="lazy";img.alt="Cached G-code preview for "+(file.filename||file.name);
   img.src=source==="local"?"/api/printer/sdcard/local-preview?path="+encodeURIComponent(file.relative_path):"/api/printer/sdcard/preview/"+encodeURIComponent(file.name);
   img.onerror=()=>{img.replaceWith(Object.assign(document.createElement("div"),{className:"model-placeholder",textContent:"▧"}));};
  }else{img.className="model-placeholder";img.textContent="▧";}
  const name=document.createElement("strong");name.textContent=file.filename||file.name;
  const info=document.createElement("small");const meta=file.metadata||file.local_metadata?.metadata||{};
  info.textContent=(source==="local"?"Mounted G-code":"Printer listing")+(meta.estimated_print_time?" · "+meta.estimated_print_time:"")+(file.source_present===false?" · cached/offline":"");
  button.append(img,name,info);
  const select=()=>{document.querySelectorAll(".model").forEach(el=>el.classList.remove("selected"));button.classList.add("selected");state.selected=file;text("selected-file",(file.relative_path||file.name)+" · preview only; open full Observatory for guarded actions");};
  button.addEventListener("click",select);button.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();select();}});
  return button;
 }
 async function loadFiles(){
  if(state.loading)return;state.loading=true;const host=$("models");
  try{
   const listing=await api("/api/printer/sdcard");
   const local=listing.local_index?.files||[];const printer=(listing.entries||[]).filter(e=>e.kind==="file");
   host.replaceChildren();const seen=new Set();
   for(const item of local){const key=item.relative_path;if(!key||seen.has(key))continue;seen.add(key);host.append(card(item,"local"));}
   for(const item of printer){if(!item.name)continue;const matched=local.some(x=>x.firmware_key===item.name.toLowerCase()&&x.source_present!==false);if(matched)continue;host.append(card(item,"printer"));}
   text("model-count",host.childElementCount+" files · "+(listing.local_index?.status||listing.status||"unknown"));
   if(!host.childElementCount){const empty=document.createElement("div");empty.className="tile";empty.textContent=listing.local_index?.error||listing.error||"No indexed G-code available. Configure a read-only source volume or refresh the printer SD listing.";host.append(empty);}
  }catch(e){if(e.message!=="Authentication required"){host.replaceChildren();const error=document.createElement("div");error.className="tile";error.textContent="File listing unavailable: "+e.message;host.append(error);text("model-count","Unavailable");}}
  finally{state.loading=false;}
 }
 $("refresh-files").addEventListener("click",loadFiles);
 $("login-form").addEventListener("submit",async e=>{e.preventDefault();text("login-error","");try{const res=await fetch("/api/auth/session",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify({token:$("token").value})});if(!res.ok)throw new Error("Access token rejected");$("token").value="";$("login").classList.remove("visible");state.login=false;await refresh();}catch(err){text("login-error",err.message);}});
 async function refresh(){
  if(document.hidden||state.login)return;
  try{const s=await api("/api/printer/status");renderStatus(s);}catch(err){if(err.message!=="Authentication required"){text("health","● API unavailable");$("health").dataset.state="offline";text("integrity","Unavailable");text("observations-note",err.message);}}
  try{renderChart(await api("/api/printer/temperatures"));}catch(err){if(err.message!=="Authentication required")text("observations-note","Temperature history unavailable: "+err.message);}
 }
 document.addEventListener("visibilitychange",()=>{if(!document.hidden)refresh();});
 refresh();state.interval=setInterval(refresh,5000);
})();
