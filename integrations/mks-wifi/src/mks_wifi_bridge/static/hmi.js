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
 let routeAnimation=null,leaveAnimation=null,departingPanel=null;
 function finishDeparture(){if(departingPanel){for(const key of ['display','position','inset','zIndex','pointerEvents'])departingPanel.style[key]='';departingPanel.inert=false;departingPanel=null;}}
 function route(view){if(!["overview","print-bay","models","observations","controls","settings"].includes(view))return;leaveAnimation?.cancel();finishDeparture();const prior=state.view,oldPanel=$('view-'+prior),oldDisplay=getComputedStyle(oldPanel).display;state.view=view;document.querySelectorAll(".view").forEach(el=>el.classList.toggle("active",el.id==="view-"+view));document.querySelectorAll("[data-go]").forEach(el=>{if(el.tagName==="BUTTON"){if(el.dataset.go===view)el.setAttribute("aria-current","page");else el.removeAttribute("aria-current");}});if(prior!==view&&!matchMedia('(prefers-reduced-motion: reduce)').matches){routeAnimation?.cancel();const panel=$('view-'+view);const order=['overview','print-bay','models','observations','controls'];const direction=order.indexOf(view)>order.indexOf(prior)?1:-1;departingPanel=oldPanel;Object.assign(oldPanel.style,{display:oldDisplay,position:'absolute',inset:'0',zIndex:'2',pointerEvents:'none'});oldPanel.inert=true;leaveAnimation=oldPanel.animate([{opacity:1,transform:'translate3d(0,0,0)'},{opacity:0,transform:`translate3d(${-direction*22}px,-4px,0) scale(.985)`}],{duration:230,easing:'cubic-bezier(.4,0,1,1)'});leaveAnimation.finished.then(finishDeparture,()=>{});routeAnimation=panel.animate([{opacity:0,transform:`translate3d(${direction*34}px,8px,0) scale(.975)`,filter:'blur(3px)'},{opacity:1,transform:'translate3d(0,0,0) scale(1)',filter:'blur(0)'}],{duration:480,easing:'cubic-bezier(.16,1,.3,1)'});}if(view==="models")loadFiles();if(view==="controls"&&!$("operations-frame").getAttribute("src"))$("operations-frame").src="/?presentation=hmi-operations&view=machine";}
 document.querySelectorAll("[data-go]").forEach(b=>b.addEventListener("click",()=>route(b.dataset.go)));
 let gesture=null;
 document.querySelector("main").addEventListener("pointercancel",()=>{gesture=null;});
 document.querySelector("main").addEventListener("pointerdown",e=>{if(e.pointerType==="touch"&&!e.target.closest("#models,button,a,input,canvas,dialog,iframe"))gesture={x:e.clientX,y:e.clientY};});
 document.querySelector("main").addEventListener("pointerup",e=>{if(!gesture)return;const dx=e.clientX-gesture.x,dy=e.clientY-gesture.y;gesture=null;if(Math.abs(dx)>90&&Math.abs(dx)>Math.abs(dy)*1.6){const views=["overview","print-bay","models","observations","controls","settings"];route(views[Math.max(0,Math.min(4,views.indexOf(state.view)+(dx<0?1:-1)))]);}});
 let previouslyLive=false,previouslyError=false;
 function renderStatus(s){
  window.dispatchEvent(new CustomEvent('hmi:telemetry',{detail:s}));
  const online=Boolean(s.connection?.connected)&&!s.stale;
  const machineError=/error|fault/i.test(s.telemetry?.printer_state?.value||'');if((previouslyLive&&!online)||(machineError&&!previouslyError))window.dispatchEvent(new CustomEvent('hmi:interrupt'));previouslyLive=online;previouslyError=machineError;
  const cinema=$('cinema-telemetry');if(cinema)cinema.textContent=(online?'Live':'Telemetry '+(s.stale?'stale':'unavailable'))+' · Nozzle '+value(s.telemetry?.nozzle_temperature)+' · Bed '+value(s.telemetry?.bed_temperature);
  const stateName=online?"live":s.stale?"stale":"offline";
  $("health").dataset.state=stateName;
  text("health","● "+(online?"Connected":s.stale?"Stale / unavailable":"Offline"));
  text("age",s.data_age_seconds==null?"No observation":Math.round(s.data_age_seconds)+"s old");
  const t=s.telemetry||{};
  text("nozzle",value(t.nozzle_temperature));text("bed",value(t.bed_temperature));
  text("nozzle-large",value(t.nozzle_temperature));text("bed-large",value(t.bed_temperature));
  const job=s.print_job||{};
  text("job-state",job.status==="unknown"?"SD status unverified":job.status||"Awaiting job status");
  text("job-file",job.filename?.status==="available"?job.filename.value:"No verified printer SD job");
  const percent=job.progress_percent?.status==="available"?number(job.progress_percent.value):null;
  $("progress").hidden=percent===null;
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

 let inspectorViewer=null;
 let inspectorEpoch=0;
 let inspected=null;
 const modelUrl=(file,view="iso")=>"/api/hmi/model-preview?path="+encodeURIComponent(file.relative_path)+"&view="+encodeURIComponent(view);
 const legacyPreview=(file,source)=>source==="local"
   ?"/api/printer/sdcard/local-preview?path="+encodeURIComponent(file.relative_path)
   :"/api/printer/sdcard/preview/"+encodeURIComponent(file.name);
 function setPreviewImage(img,file,source){
   img.loading="lazy";
   img.alt="Shaded extrusion geometry for "+(file.filename||file.name);
   if(source==="local"&&file.source_present!==false) {
     img.src=modelUrl(file);
     img.onerror=()=>{
       img.onerror=null;
       if(file.preview_available)img.src=legacyPreview(file,source);
       else img.replaceWith(Object.assign(document.createElement("div"),{className:"model-placeholder",textContent:"▧"}));
     };
   }else if(file.preview_available){
     img.src=legacyPreview(file,source);
     img.onerror=()=>img.replaceWith(Object.assign(document.createElement("div"),{className:"model-placeholder",textContent:"▧"}));
   }else{
     img.replaceWith(Object.assign(document.createElement("div"),{className:"model-placeholder",textContent:"▧"}));
   }
 }
 function closeInspector(){
   inspectorEpoch++;
   if(inspectorViewer){inspectorViewer.dispose();inspectorViewer=null;}
   $("model-viewer-host").replaceChildren();
   $("model-viewer-host").hidden=true;
   $("inspector-image").hidden=false;
   const dialog=$("model-inspector");
   if(dialog.open)dialog.close();
 }
 $("inspector-close").addEventListener("click",closeInspector);
 $("model-inspector").addEventListener("cancel",e=>{e.preventDefault();closeInspector();});
 document.querySelectorAll("[data-view-angle]").forEach(button=>button.addEventListener("click",()=>{
   const view=button.dataset.viewAngle;
   if(inspectorViewer)inspectorViewer.setView(view);
   else if(inspected?.source==="local"&&inspected.file.source_present!==false) $("inspector-image").src=modelUrl(inspected.file,view);
 }));
 async function openInspector(file,source){
   closeInspector();
   const epoch=++inspectorEpoch;
   inspected={file,source};
   text("inspector-title",file.filename||file.name);
   text("inspector-details","G-code deposited filament · geometry approximation, not a CAD solid.");
   text("inspector-status",source==="local"?"Loading 3D extrusion…":"No local G-code geometry available; showing cached slicer preview.");
   const preview=$("inspector-image");
   preview.hidden=false;
   preview.onerror=null;
   if(source==="local"&&file.source_present!==false){
     preview.src=modelUrl(file);
     preview.onerror=()=>{preview.onerror=null;if(file.preview_available)preview.src=legacyPreview(file,source);};
   }else if(file.preview_available)preview.src=legacyPreview(file,source);
   else preview.removeAttribute("src");
   $("model-inspector").showModal();
   if(source!=="local"||file.source_present===false)return;
   try{
     const geometry=await api("/api/hmi/model-geometry?path="+encodeURIComponent(file.relative_path));
     if(epoch!==inspectorEpoch)return;
     const module=await import("/static/hmi_model_viewer.js");
     if(epoch!==inspectorEpoch)return;
     const host=$("model-viewer-host");
     host.hidden=false;
     inspectorViewer=module.mountViewer(host,geometry);
     preview.hidden=true;
     const stats=geometry.stats||{};
     const bounds=geometry.bounds||[[0,0,0],[0,0,0]];
     const dims=bounds[1].map((v,i)=>(v-bounds[0][i]).toFixed(1)).join(" × ");
     text("inspector-details",dims+" mm · "+(stats.rendered_segments||0)+" rendered extrusion segments");
     text("inspector-status","Interactive 3D · sampled from "+(stats.extrusion_segments||0)+" extrusion moves"+
       (stats.arcs_represented_as_chords?" · "+stats.arcs_represented_as_chords+" arcs approximated":""));
   }catch(err){
     if(epoch!==inspectorEpoch)return;
     $("model-viewer-host").hidden=true;
     preview.hidden=false;
     text("inspector-status","3D inspector unavailable: "+err.message+" · still-image fallback");
   }
 }
 function bindTouchActivation(target,callback){
  let touch=null,lastActivation=-Infinity;
  const activate=()=>{
   const now=performance.now();
   if(now-lastActivation<500)return;
   lastActivation=now;
   callback();
  };
  target.addEventListener("pointerdown",e=>{
   if(e.pointerType==="touch")touch={id:e.pointerId,x:e.clientX,y:e.clientY};
  });
  target.addEventListener("pointercancel",()=>{touch=null;});
  target.addEventListener("pointerup",e=>{
   const start=touch;touch=null;
   if(start&&e.pointerType==="touch"&&start.id===e.pointerId&&
      Math.hypot(e.clientX-start.x,e.clientY-start.y)<12)activate();
  });
  target.addEventListener("click",activate);
  return activate;
 }
 function card(file,source){
   const button=document.createElement("div");
   button.className="model";button.tabIndex=0;button.setAttribute("role","button");
   button.setAttribute("aria-label","Inspect 3D print "+(file.filename||file.name));
   const image=document.createElement("img");
   const name=document.createElement("strong");name.textContent=file.filename||file.name;
   const info=document.createElement("small");
   const meta=file.metadata||file.local_metadata?.metadata||{};
   info.textContent=(source==="local"?"Local G-code geometry":"Printer SD listing")+
     (meta.estimated_print_time?" · "+meta.estimated_print_time:"")+
     (file.source_present===false?" · cached/offline":"");
   button.append(image,name,info);
   setPreviewImage(image,file,source);
   if(source==="local"&&file.source_present!==false){
     const pill=document.createElement("span");pill.className="render-pill";pill.textContent="3D G-CODE";button.append(pill);
   }
   const select=()=>{
     document.querySelectorAll(".model").forEach(el=>el.classList.remove("selected"));
     button.classList.add("selected");
     state.selected=file;
     text("selected-file",(file.relative_path||file.name)+" · read-only model inspection");
     openInspector(file,source);
   };
   const activate=bindTouchActivation(button,select);
   button.addEventListener("keydown",e=>{
     if(e.key==="Enter"||e.key===" "){e.preventDefault();activate();}
   });
   return button;
 }
 function filterModels(){
  const q=($('model-filter')?.value||'').trim().toLowerCase();
  const models=Array.from($('models').querySelectorAll('.model'));
  let shown=0;for(const item of models){item.hidden=!!q&&!item.textContent.toLowerCase().includes(q);if(!item.hidden)shown++;}
  if(models.length)text('model-count',shown+' of '+models.length+' files');
 }
 window.addEventListener('hmi:filter-models',filterModels);
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
  finally{state.loading=false;filterModels();}
 }
 function openManagedFiles(){
  $("operations-frame").src="/?presentation=hmi-operations&view=sdcard";
  route("controls");
  closeInspector();
 }
 bindTouchActivation($("manage-model"),openManagedFiles);
 $("refresh-files").addEventListener("click",loadFiles);
 let hmiImportEnabled=false,uploading=false;
 async function checkImportStatus(){
  try{
   const result=await api('/api/hmi/import-status');
   hmiImportEnabled=Boolean(result.available);
   text('sd-import-status',hmiImportEnabled?'Ready · OctoPrint local':result.note||'Import disabled');
   $('import-gcode').title=result.note||'Import local G-code without printing';
  }catch(error){
   hmiImportEnabled=false;
   if(error.message!=='Authentication required')text('sd-import-status','Import status unavailable');
  }
 }
 $('import-gcode').addEventListener('click',()=>{
  if(uploading)return;
  if(!hmiImportEnabled){
   text('sd-import-status','Enable NEPTUNE_HMI_IMPORT_ENABLED=true, redeploy');
   return;
  }
  $('import-gcode-files').click();
 });
 $('import-gcode-files').addEventListener('change',async()=>{
  if(uploading)return;
  uploading=true;$('import-gcode').disabled=true;
  const input=$('import-gcode-files'),files=[...input.files].slice(0,30);
  let imported=0,skipped=0;
  try{
   for(const file of files){
    if(!/\.(?:gcode|gco|gc|g)$/i.test(file.name)||file.size<1||file.size>50*1024*1024){
     skipped++;text('sd-import-status','Skipped invalid or oversized: '+file.name);continue;
    }
    text('sd-import-status','Importing '+file.name+'…');
    try{
     const result=await api('/api/hmi/import-gcode?filename='+encodeURIComponent(file.name),{
      method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file
     });
     if(result.status==='imported')imported++;else skipped++;
    }catch(error){
     skipped++;
     if(error.message==='Authentication required')break;
     text('sd-import-status','Import failed: '+error.message);break;
    }
   }
  }finally{
   input.value='';uploading=false;$('import-gcode').disabled=false;
   text('sd-import-status','Imported '+imported+(skipped?' · '+skipped+' skipped':'')+' · no printing');
   if(imported)await loadFiles();
  }
 });
 checkImportStatus();
 $("login-form").addEventListener("submit",async e=>{e.preventDefault();text("login-error","");try{const res=await fetch("/api/auth/session",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify({token:$("token").value})});if(!res.ok)throw new Error("Access token rejected");$("token").value="";$("login").classList.remove("visible");state.login=false;await refresh();await checkImportStatus();}catch(err){text("login-error",err.message);}});
 async function refresh(){
  if(document.hidden||state.login)return;
  try{const s=await api("/api/printer/status");renderStatus(s);}catch(err){if(err.message!=="Authentication required"){text("health","● API unavailable");$("health").dataset.state="offline";text("integrity","Unavailable");text("observations-note",err.message);}}
  try{const temperatures=await api("/api/printer/temperatures");renderChart(temperatures);window.dispatchEvent(new CustomEvent('hmi:temperatures',{detail:temperatures}));}catch(err){if(err.message!=="Authentication required")text("observations-note","Temperature history unavailable: "+err.message);}
 }
 document.addEventListener("visibilitychange",()=>{if(!document.hidden)refresh();});
 refresh();state.interval=setInterval(refresh,5000);
})();
