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
 function updateEmbeddedRendering(){const frame=$("print-bay-frame");frame?.contentWindow?.postMessage({type:"neptune:presentation",active:state.view==="print-bay"},location.origin);}
 $("print-bay-frame").addEventListener("load",updateEmbeddedRendering);
 function route(view){if(!["overview","print-bay","models","observations","controls","settings"].includes(view))return;leaveAnimation?.cancel();finishDeparture();const prior=state.view,oldPanel=$('view-'+prior),oldDisplay=getComputedStyle(oldPanel).display;state.view=view;document.querySelectorAll(".view").forEach(el=>el.classList.toggle("active",el.id==="view-"+view));document.querySelectorAll("[data-go]").forEach(el=>{if(el.tagName==="BUTTON"){if(el.dataset.go===view)el.setAttribute("aria-current","page");else el.removeAttribute("aria-current");}});if(prior!==view&&!matchMedia('(prefers-reduced-motion: reduce)').matches){routeAnimation?.cancel();const panel=$('view-'+view);const order=['overview','print-bay','models','observations','controls'];const direction=order.indexOf(view)>order.indexOf(prior)?1:-1;departingPanel=oldPanel;Object.assign(oldPanel.style,{display:oldDisplay,position:'absolute',inset:'0',zIndex:'2',pointerEvents:'none'});oldPanel.inert=true;leaveAnimation=oldPanel.animate([{opacity:1,transform:'translate3d(0,0,0)'},{opacity:0,transform:`translate3d(${-direction*22}px,-4px,0) scale(.985)`}],{duration:230,easing:'cubic-bezier(.4,0,1,1)'});leaveAnimation.finished.then(finishDeparture,()=>{});routeAnimation=panel.animate([{opacity:0,transform:`translate3d(${direction*34}px,8px,0) scale(.975)`,filter:'blur(3px)'},{opacity:1,transform:'translate3d(0,0,0) scale(1)',filter:'blur(0)'}],{duration:480,easing:'cubic-bezier(.16,1,.3,1)'});}window.dispatchEvent(new CustomEvent('hmi:view',{detail:{view}}));updateEmbeddedRendering();if(view==="models")loadFiles();if(view==="controls"&&!$("operations-frame").getAttribute("src"))$("operations-frame").src="/?presentation=hmi-operations&view=machine";}
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

 let factoryUsb={available:false,selection_enabled:false};
 async function refreshIngestionStatus(){
  try{const result=await api('/api/fabrication/ingestion/status');const adapter=result.adapters.find(item=>item.provider==='lilbitdrop');
   text('ingestion-status','LilBitDrop · '+(adapter?.state||'unavailable')+' · '+(adapter?.ingested||0)+' archived · '+(adapter?.pending||0)+' pending'+(adapter?.errors?.length?' · '+adapter.errors.at(-1).error:''));
  }catch(error){text('ingestion-status','File handoff status unavailable: '+error.message);}
 }
 refreshIngestionStatus();setInterval(()=>{if(!document.hidden&&state.view==='settings')refreshIngestionStatus();},5000);
 api("/api/factory-usb").then(value=>{factoryUsb=value;}).catch(()=>{});
 let inspectorViewer=null;
 let inspectorRequest=null;
 let inspectorEpoch=0;
 let inspected=null;
 const geometrySource=source=>["local","native_cache","archive"].includes(source);
 const hasGeometry=(file,source)=>geometrySource(source)&&file.source_present!==false&&(source!=="native_cache"||file.geometry_available===true);
 const modelUrl=(file,view="iso")=>file.engineering?"/api/model-registry/artifacts/"+file.sha256+"/thumbnail":file.archive?"/api/fabrication/artifacts/"+file.sha256+"/thumbnail?format=png":file.provider==="printer_sd"&&file.sha256?"/api/native-sd/assets/"+file.sha256+"/thumbnail":"/api/hmi/model-preview?path="+encodeURIComponent(file.relative_path)+"&view="+encodeURIComponent(view)+"&format=png";
 const legacyPreview=(file,source)=>source==="local"
   ?"/api/printer/sdcard/local-preview?path="+encodeURIComponent(file.relative_path)
   :"/api/printer/sdcard/preview/"+encodeURIComponent(file.name);
 function setPreviewImage(img,file,source){
   img.loading="lazy";
   img.alt="Shaded extrusion geometry for "+(file.filename||file.name);
   if(hasGeometry(file,source)) {
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
   inspectorRequest?.abort();inspectorRequest=null;
   if(inspectorViewer){inspectorViewer.dispose();inspectorViewer=null;}
   $("model-viewer-host").replaceChildren();$("geometry-presentation").hidden=true;
   $("model-viewer-host").hidden=true;
   $("inspector-image").hidden=false;
   const dialog=$("model-inspector");
   if(dialog.open)dialog.close();
   window.dispatchEvent(new CustomEvent("hmi:inspector",{detail:{open:false}}));
 }
 $("inspector-close").addEventListener("click",closeInspector);
 $("model-inspector").addEventListener("cancel",e=>{e.preventDefault();closeInspector();});
 document.querySelectorAll("[data-view-angle]").forEach(button=>button.addEventListener("click",()=>{
   const view=button.dataset.viewAngle;
   if(inspectorViewer)inspectorViewer.setView(view);
   else if(geometrySource(inspected?.source)&&inspected.file.source_present!==false) $("inspector-image").src=modelUrl(inspected.file,view);
 }));
 async function openInspector(file,source){
   closeInspector();
   const epoch=++inspectorEpoch;
   inspectorRequest=new AbortController();
   inspected={file,source};
   text("inspector-title",file.display_name||file.filename||file.name);$("manage-model").hidden=!!file.engineering;
   text('lifecycle-details',file.lifecycle?Object.entries(file.lifecycle).filter(([key,value])=>typeof value==='string').map(([key,value])=>key.replaceAll('_',' ')+': '+value.replaceAll('_',' ')).join(' · '):'Discovery and byte availability do not establish print outcome.');
   text("inspector-details","G-code deposited filament · geometry approximation, not a CAD solid.");
   text("inspector-status",hasGeometry(file,source)?"Loading 3D extrusion…":source==="native_cache"?(file.geometry_error||"Verified bytes · no supported deposited-filament geometry"):"No local G-code geometry available; showing cached slicer preview.");
   $('execution-quality').hidden=!(file.archive&&(file.started_at||file.archive_collection==='history'));
   text('execution-record','');
   $('execution-identity').hidden=!(file.archive_collection==='history'&&!file.sha256);
   if(!$('execution-identity').hidden)api('/api/fabrication/library/executables').then(result=>{if(epoch!==inspectorEpoch)return;$('execution-artifact').replaceChildren(Object.assign(document.createElement('option'),{value:'',textContent:'Select archived original G-code'}),...result.items.map(item=>Object.assign(document.createElement('option'),{value:item.sha256,textContent:item.filename+' · '+item.sha256.slice(0,10)})));}).catch(error=>text('execution-record',error.message));

   if(file.archive&&(file.started_at||file.archive_collection==='history'))api('/api/fabrication/executions/'+encodeURIComponent(file.id)+'/detail',{signal:inspectorRequest.signal}).then(record=>{
    if(epoch!==inspectorEpoch)return;
    $('observation-list').replaceChildren(...(record.attachments||[]).map(item=>{const link=document.createElement('a');link.href='/api/fabrication/artifacts/'+item.sha256+'/observation';link.textContent=item.kind+' · '+(item.filename||item.sha256.slice(0,12));link.style.display='block';return link;}));
    const latest=record.quality_assessments?.[0];$('quality-result').value=latest?.result||'unassessed';$('quality-notes').value=latest?.notes||'';
    text('execution-record','Observed events: '+(record.events||[]).slice(-8).map(event=>event.observed_at+' '+(event.payload?.state||event.event_type)).join(' · '));
   }).catch(error=>{if(epoch===inspectorEpoch&&error.name!=='AbortError')text('execution-record','Execution detail unavailable: '+error.message);});
   const preview=$("inspector-image"),empty=$('inspector-empty');
   preview.hidden=true;empty.hidden=false;preview.onerror=null;preview.removeAttribute('src');
   text('inspector-empty-title',hasGeometry(file,source)?'Preparing geometry':'Original bytes unavailable');
   text('inspector-empty-note',hasGeometry(file,source)?'Reading verified source geometry…':'This directory entry has no acquired G-code. Archive an original through upload, LilBitDrop or an external SD reader. USB selection does not download its contents.');
   const showPreview=url=>{preview.onerror=()=>{preview.hidden=true;empty.hidden=false;text('inspector-empty-title','Preview unavailable');};preview.onload=()=>{if(epoch!==inspectorEpoch)return;preview.hidden=false;empty.hidden=true;};preview.src=url;};
   if(hasGeometry(file,source))showPreview(modelUrl(file));
   else if(file.preview_available)showPreview(legacyPreview(file,source));
   $('archive-original').hidden=hasGeometry(file,source);
   document.querySelector('.inspector-actions').style.display=hasGeometry(file,source)?'flex':'none';$('geometry-guide').hidden=!hasGeometry(file,source);
   document.querySelectorAll('[data-view-angle]').forEach(button=>button.disabled=!hasGeometry(file,source));
   if(!hasGeometry(file,source)){text('inspector-status','Metadata only · no reconstructed model');text('inspector-details','Directory or history observation · original bytes not acquired');}
   $("print-again").hidden=!(file.archive&&file.bytes_available&&/\.(gcode|gco|gc|g)$/i.test(file.filename||file.name||""));
   $("print-again").textContent=file.started_at?"Print again · stage copy":"Stage archived artifact";
   if(file.archive)text("inspector-details",[file.state||"Unprinted",file.started_at||file.ingested_at,file.ended_at||"Outcome not recorded",file.source||"Archived bytes"].join(" · "));
   $("factory-select-file").hidden=!["printer","printer_native"].includes(source);
   $("factory-select-file").disabled=!(factoryUsb.available&&factoryUsb.selection_enabled);
   $("retrieve-sd").hidden=source!=="printer_native"||!file.retrievable;
   $("cancel-sd").hidden=true;
   $("copy-sd-local").hidden=source!=="native_cache";
   text("native-sd-status",source==="printer_native"?"Retrieve actual SD bytes before reconstruction.":"");
   $('slicer-detail').hidden=!(file.source_project_sha||file.metadata?.source_3mf);
   $('slicer-metadata').replaceChildren();
   const sourceProject=file.source_project_sha||file.metadata?.source_3mf;
   if(sourceProject)api('/api/fabrication/slicer-projects/'+sourceProject,{signal:inspectorRequest.signal}).then(project=>{
    if(epoch!==inspectorEpoch)return;
    const root=$('slicer-metadata');const description=document.createElement('p');description.textContent=project.bodies.length+' extracted bodies · '+project.plates.length+' plates · '+project.units+' · Original 3MF archived; model identity/authority unresolved';root.append(description);
    for(const plate of project.plates){const row=document.createElement('p');row.textContent=plate.name+' · '+plate.instances.length+' placed instances';root.append(row);}
    const link=document.createElement('a');link.href='/api/fabrication/artifacts/'+sourceProject+'/original';link.textContent='Download original 3MF';root.append(link);
    const settings=document.createElement('pre');settings.style.cssText='font-size:10px;white-space:pre-wrap;overflow-wrap:anywhere';settings.textContent=JSON.stringify(project.settings,null,2);root.append(settings);
   }).catch(error=>{if(error.name!=='AbortError')text('slicer-metadata',error.message);});
   $('model-tools').hidden=!(file.sha256||file.model_id);$('model-tools').open=!!file.model_id;
   if(file.sha256||file.model_id)populateRegistry(file);
   $("model-inspector").showModal();
   window.dispatchEvent(new CustomEvent("hmi:inspector",{detail:{open:true}}));
   if(!hasGeometry(file,source))return;
   try{
     const geometry=await api(file.engineering?"/api/model-registry/artifacts/"+file.sha256+"/geometry":source==="archive"?"/api/fabrication/artifacts/"+file.sha256+"/geometry":source==="native_cache"&&file.sha256?"/api/native-sd/assets/"+file.sha256+"/geometry":"/api/hmi/model-geometry?path="+encodeURIComponent(file.relative_path),{signal:inspectorRequest.signal});
     if(epoch!==inspectorEpoch)return;
     const module=await import("/static/hmi_model_viewer.js");
     if(epoch!==inspectorEpoch)return;
     const host=$("model-viewer-host");
     host.hidden=false;
     const viewer=inspectorViewer=module.mountViewer(host,geometry);
     const ready=await viewer.ready;
     if(epoch!==inspectorEpoch||!ready)return;
     preview.hidden=true;empty.hidden=true;
     $('geometry-presentation').hidden=!!file.engineering;$('geometry-mode').value='shaded';$('geometry-range').value='100';viewer.setPresentation('shaded',1);
     const stats=geometry.stats||{};
     const bounds=geometry.bounds||[[0,0,0],[0,0,0]];
     const dims=bounds[1].map((v,i)=>(v-bounds[0][i]).toFixed(1)).join(" × ");
     if(geometry.surface_vertices){
      text('inspector-details',dims+(geometry.units==='mm'?' mm · 3MF-declared units; not physical measurement':' source coordinate units · units unverified'));
      text('inspector-status',(geometry.source==='derived_3mf_surface'?'3MF body surface':'Original STL surface')+' · '+geometry.stats.rendered_triangles+' triangles · fingerprint verified'+(geometry.display?.method==='vertex_clustering'?' · optimized display mesh':''));
     }else{
     text("inspector-details",dims+" mm · "+(stats.rendered_segments||0)+" rendered extrusion segments");
     text("inspector-status","Interactive 3D · sampled from "+(stats.extrusion_segments||0)+" extrusion moves"+
       (stats.arcs_represented_as_chords?" · "+stats.arcs_represented_as_chords+" arcs approximated":""));
     }
   }catch(err){
     if(epoch!==inspectorEpoch)return;
     $("model-viewer-host").hidden=true;
     preview.hidden=!(preview.complete&&preview.naturalWidth);empty.hidden=!preview.hidden;
     text('inspector-empty-title','Geometry unavailable');text('inspector-empty-note',err.message);
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
   button.className="model";button.dataset.execution=file.state||"unknown";button.dataset.provider=file.archive?$("storage-provider").value:source==="printer_native"||source==="printer"?"printer_sd":source;button.tabIndex=0;button.setAttribute("role","button");
   button.setAttribute("aria-label","Inspect 3D print "+(file.filename||file.name));
   const image=document.createElement("img");
   const name=document.createElement("strong");name.textContent=file.display_name||file.filename||file.name;
   const info=document.createElement("small");
   const meta=file.metadata||file.print_metadata||file.local_metadata?.metadata||{};
   info.textContent=file.archive?[file.archive_collection==="discovered"?"History unknown":file.started_at?"Reported "+(file.state||"unknown"):"No recorded executions",file.started_at||file.ingested_at,file.bytes_available?"Archived original":"Bytes unavailable"].join(" · "):(source==="native_cache"?"Verified SD cache · SHA-256 "+file.sha256.slice(0,10):source==="local"?"OctoPrint / local G-code":"Printer SD · "+(file.retrievable?"retrievable bytes":"bytes unavailable"))+
     (meta.estimated_print_time?" · "+meta.estimated_print_time:"")+
     (Number.isFinite(file.size_bytes??file.size)?" · "+Number(file.size_bytes??file.size).toLocaleString()+" B":"")+
     (file.source_present===false?" · cached/offline":"");
   if(file.archive_collection)info.textContent=file.archive_collection==='history'?(file.state||'Unknown'):file.archive_collection==='discovered'?'Metadata only':file.bytes_available?'Archived':'Source only';
   if(file.model_id)info.textContent=(file.identity_state==='candidate'?'Candidate · ':'')+'Revision '+(file.label||'unresolved')+' · '+(file.approved?'Scoped authority approved':'Authority unresolved');
   else if(file.artifact_kind)info.textContent=file.artifact_kind.replaceAll('_',' ')+' · '+(file.bytes_available?'Archived':'Not archived');
   button.append(image,name,info);
   setPreviewImage(image,file,source);
   if(hasGeometry(file,source)){
     const pill=document.createElement("span");pill.className="render-pill";pill.textContent=file.source_project_sha?"3MF PLATE":file.metadata?.source_3mf?"3MF BODY":file.engineering?"SOURCE STL":"3D G-CODE";button.append(pill);
   }
   const select=()=>{
     document.querySelectorAll(".model").forEach(el=>el.classList.remove("selected"));
     button.classList.add("selected");
     state.selected=file;
     if(file.archive_collection)localStorage.setItem("neptune.archive.selection",file.archive_collection+":"+file.id);
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
  let shown=0;for(const item of models){item.hidden=!!q&&!item.textContent.toLowerCase().includes(q);if($('storage-provider').value!=='all'&&item.dataset.provider!==$('storage-provider').value)item.hidden=true;if(!item.hidden)shown++;}
  if(models.length)text('model-count',shown+' of '+models.length+' files');
 }
 window.addEventListener('hmi:filter-models',filterModels);
 $('storage-provider').addEventListener('change',()=>{localStorage.setItem('neptune.gallery.provider',$('storage-provider').value);filterModels();loadFiles();});
 const savedProvider=localStorage.getItem('neptune.gallery.provider');if([...$('storage-provider').options].some(option=>option.value===savedProvider))$('storage-provider').value=savedProvider;
 async function registryAction(operation,data={}){return api('/api/model-registry/actions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operation,...data})});}
 let registryEpoch=0;
 async function populateRegistry(file){
  const epoch=++registryEpoch;
  try{const data=await api('/api/model-registry/models');if(epoch!==registryEpoch)return;
   $('registry-model').replaceChildren(...data.items.map(model=>{const option=document.createElement('option');option.value=model.id;option.textContent=model.name;return option;}));
   if(file.model_id)$('registry-model').value=file.model_id;
   const role=/\.(gcode|gco|gc|g)$/i.test(file.filename||'')?'executable_gcode':/\.3mf$/i.test(file.filename||'')?'slicer_configuration':/\.(stl|step|stp|obj)$/i.test(file.filename||'')?'manufacturing_geometry':'native_design';$('registry-role').value=role;
   for(const id of ['registry-attach','registry-candidate','registry-approve'])$(id).disabled=!file.sha256;
   $('registry-model').onchange=()=>loadRegistryRevisions(file);
   await loadRegistryRevisions(file);
  }catch(error){text('registry-detail',error.message);}
 }
 async function loadRegistryRevisions(file){
  const identity=$('registry-model').value;if(!identity){text('registry-detail','Create a logical model to reconcile this artifact. No model identity is inferred from its filename.');return;}
  const epoch=++registryEpoch;
  try{const data=await api('/api/model-registry/models/'+identity);if(epoch!==registryEpoch)return;
   $('registry-revision').replaceChildren(...data.revisions.map(item=>{const option=document.createElement('option');option.value=item.revision.id;option.textContent=item.revision.label;return option;}));
   if(file.revision_id)$('registry-revision').value=file.revision_id;
   const show=()=>{const selected=data.revisions.find(item=>item.revision.id===$('registry-revision').value);$('registry-authorities').replaceChildren();if(!selected)return;
    text('registry-detail','Revision '+selected.revision.id+' · '+selected.representations.length+' representations · '+selected.components.length+' components · '+selected.parents.length+' parent revisions. '+(file.locations||[]).map(item=>item.path).join(' · '));
    for(const authority of selected.authorities){const row=document.createElement('p');row.textContent=authority.role+' · '+authority.state+' · '+authority.sha256.slice(0,12);if(authority.state!=='superseded'){const button=document.createElement('button');button.textContent='Retire declaration';button.onclick=async()=>{try{await registryAction('retire_authority',{identity:authority.id});await loadRegistryRevisions(file);}catch(error){text('registry-detail',error.message);}};row.append(button);}$('registry-authorities').append(row);}
   };$('registry-revision').onchange=show;show();
  }catch(error){text('registry-detail',error.message);}
 }
 $('new-model').addEventListener('click',async()=>{const name=prompt('Logical model name (independent of filename or directory):');if(!name)return;const label=prompt('Initial design revision label:','Working revision');if(!label)return;
  try{const model=await registryAction('create_model',{name});await registryAction('create_revision',{model_id:model.id,label});$('storage-provider').value='models';loadFiles();}catch(error){text('model-count',error.message);}
 });
 async function representCurrent(approve){
  if(!inspected?.file?.sha256)return;const revision_id=$('registry-revision').value;const role=$('registry-role').value;if(!revision_id)throw new Error('Select a model revision first');
  const args={revision_id,sha:inspected.file.sha256,role};
  if(approve===true&&!confirm('Approve this exact fingerprint for '+role+' in the selected model revision? Other responsibilities and revisions are unchanged.'))return;
  await registryAction('representation',{...args,active:true});
  if(approve!==null)await registryAction('authority',{...args,approve});
  await loadRegistryRevisions(inspected.file);
 }
 for(const [id,value] of [['registry-attach',null],['registry-candidate',false],['registry-approve',true]])$(id).addEventListener('click',()=>representCurrent(value).catch(error=>text('registry-detail',error.message)));
 $('registry-detach').onclick=async()=>{if(!inspected?.file?.sha256)return;try{await registryAction('representation',{revision_id:$('registry-revision').value,sha:inspected.file.sha256,role:$('registry-role').value,active:false});await loadRegistryRevisions(inspected.file);}catch(error){text('registry-detail',error.message);}};
 $('registry-hydrate').onclick=async()=>{const file=inspected?.file;if(!file?.sha256||!confirm('Archive a verified copy of this exact source? The original is unchanged; no staging or printing.'))return;try{await api('/api/model-registry/artifacts/'+file.sha256+'/hydrate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirmation:'ARCHIVE '+file.sha256})});text('registry-detail','Original bytes archived and verified. Source unchanged.');}catch(error){text('registry-detail',error.message);}};
 $('registry-component').onclick=async()=>{const component=prompt('Exact component revision ID (from its model revision):');if(!component)return;const label=prompt('Component role or label:');if(!label)return;try{await registryAction('component',{assembly:$('registry-revision').value,component,label});await loadRegistryRevisions(inspected.file);}catch(error){text('registry-detail',error.message);}};
 $('registry-revision-new').onclick=async()=>{const label=prompt('New branch revision label:');if(!label)return;try{await registryAction('create_revision',{model_id:$('registry-model').value,label,parents:[$('registry-revision').value]});await loadRegistryRevisions(inspected.file);}catch(error){text('registry-detail',error.message);}};
 $('registry-baseline').onclick=async()=>{if(!confirm('Approve an immutable baseline of the selected revision and exact authority/component references?'))return;try{const result=await registryAction('approve_baseline',{revision_id:$('registry-revision').value});if(inspected?.file)inspected.file.baseline_id=result.id;text('registry-detail','Approved baseline '+result.id);}catch(error){text('registry-detail',error.message);}};
 $('registry-manifest').onclick=async()=>{try{const data=await api('/api/model-registry/revisions/'+$('registry-revision').value+'/manifest');const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const link=document.createElement('a');link.href=url;link.download=data.revision.id+'.model.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(error){text('registry-detail',error.message);}};
 $('registry-manifest-import').onclick=()=>$('registry-manifest-file').click();
 $('registry-manifest-file').onchange=async()=>{const file=$('registry-manifest-file').files[0];if(!file)return;try{if(file.size>65536)throw Error('Manifest exceeds declaration limit');const result=await registryAction('import_manifest',{manifest:JSON.parse(await file.text()),approve:false});await populateRegistry({...inspected.file,model_id:result.model_id,revision_id:result.revision_id});text('registry-detail','Declarations imported as candidates; no authority approved or original file changed');}catch(error){text('registry-detail',error.message);}finally{$('registry-manifest-file').value='';}};
 const registryPages={models:{before:null,previous:[]},unassigned:{before:null,previous:[]}};
 const registryPager=document.createElement('span');registryPager.hidden=true;registryPager.style.cssText='display:none;gap:3px';
 const registryPrevious=document.createElement('button'),registryNext=document.createElement('button');
 for(const [button,label,text] of [[registryPrevious,'Previous registry page','←'],[registryNext,'Next registry page','→']]){button.textContent=text;button.setAttribute('aria-label',label);registryPager.append(button);}
 $('model-count').after(registryPager);
 let registrySearchTimer;
 $('model-filter').addEventListener('input',()=>{if(!registryPages[$('storage-provider').value])return;clearTimeout(registrySearchTimer);registrySearchTimer=setTimeout(()=>{for(const page of Object.values(registryPages)){page.before=null;page.previous=[];}loadFiles();},200);});
 const archivePages=Object.fromEntries(['history','unprinted','discovered'].map(kind=>[kind,{before:null,previous:[],scroll:0} ]));
 async function renderArchiveLane(kind,lane){
  const page=archivePages[kind];
  lane.querySelectorAll(".archive-pages button").forEach(button=>button.disabled=true);
  const response=await api('/api/fabrication/'+kind+'?limit=20'+(page.before?'&before='+encodeURIComponent(page.before):''));
  const track=lane.querySelector('.archive-track');track.replaceChildren();
  for(const item of response.items){const node=card({...item,archive:true,filename:item.filename||item.archived_filename||'Unknown artifact',archive_collection:kind,source_present:item.bytes_available},'archive');
   if(localStorage.getItem('neptune.archive.selection')===kind+':'+item.id)node.classList.add('selected');
   track.append(node);
  }
  if(!response.items.length)track.textContent=kind==='history'?'No observed executions yet. Outcomes are never invented.':kind==='unprinted'?'No unprinted archived artifacts yet.':'No printer SD observations yet.';
  const [previous,next]=lane.querySelectorAll('.archive-pages button');
  previous.disabled=!page.previous.length;next.disabled=!response.next;
  track.scrollLeft=page.scroll||0;
  previous.onclick=async()=>{page.scroll=0;page.before=page.previous.pop();await renderArchiveLane(kind,lane);};
  next.onclick=async()=>{page.scroll=0;page.previous.push(page.before);page.before=response.next;await renderArchiveLane(kind,lane);};
 }
 async function renderChronology(host){
  host.dataset.layout='chronology';host.replaceChildren();const pending=[];
  for(const [kind,title] of [['history','Print history'],['unprinted','Unprinted'],['discovered','SD discovery']]){
   const lane=document.createElement('section');lane.className='archive-lane';lane.dataset.collection=kind;
   const heading=document.createElement('h3');heading.textContent=title;
   const track=document.createElement('div');track.className='archive-track';track.setAttribute('aria-label',title+' chronological previews');track.addEventListener('scroll',()=>{archivePages[kind].scroll=track.scrollLeft;},{passive:true});
   const pages=document.createElement('div');pages.className='archive-pages';
   for(const [label,symbol] of [['Previous page','←'],['Next page','→']]){const button=document.createElement('button');button.textContent=symbol;button.setAttribute('aria-label',title+' '+label);pages.append(button);}
   lane.append(heading,track,pages);host.append(lane);pending.push(renderArchiveLane(kind,lane));
  }
  await Promise.all(pending);
 }
 async function loadFiles(){
  if(state.loading)return;state.loading=true;const host=$("models");const requestedProvider=$("storage-provider").value,requestedFilter=$("model-filter").value;
  try{
   const collection=$('storage-provider').value;registryPager.style.display=registryPages[collection]?'inline-flex':'none';$('archive-files').hidden=!!registryPages[collection];
   $('new-model').hidden=!['models','unassigned'].includes(collection);
   if(['models','unassigned'].includes(collection)){
    delete host.dataset.layout;host.replaceChildren();
    const pagination=registryPages[collection];
    const query=new URLSearchParams({limit:'24',q:requestedFilter});if(collection==='unassigned')query.set('unassigned','true');if(pagination.before)query.set('before',pagination.before);
    const result=await api('/api/model-registry/'+(collection==='models'?'models':'artifacts')+'?'+query);
    registryPrevious.disabled=!pagination.previous.length;registryNext.disabled=!result.next;
    registryPrevious.onclick=()=>{pagination.before=pagination.previous.pop();loadFiles();};registryNext.onclick=()=>{pagination.previous.push(pagination.before);pagination.before=result.next;loadFiles();};
    for(const row of result.items){
     const file=collection==='models'?{...row.preview,model_id:row.id,identity_state:row.identity_state,display_name:row.name,filename:row.preview?.filename||row.name,sha256:row.preview?.sha256}:row;
     const stl=/\.stl$/i.test(file.filename||'');const gcode=/\.(gcode|gco|gc|g)$/i.test(file.filename||'')&&file.archived===1;
     host.append(card({...file,archive:true,engineering:stl,source_present:stl||gcode,bytes_available:!!file.archived,geometry_available:stl||gcode},'archive'));
    }
    if(!result.items.length)host.textContent=collection==='models'?'No declared models yet. Review unassigned files and create a logical model.':'No fingerprinted engineering inventory loaded. Source files remain untouched.';
    return;
   }

   if(collection==='slicer_projects'){
    delete host.dataset.layout;host.replaceChildren();const result=await api('/api/fabrication/slicer-projects');
    for(const row of result.items)host.append(card({filename:row.filename,display_name:row.filename,sha256:row.preview_sha256,source_project_sha:row.sha256,engineering:true,archive:true,source_present:!!row.preview_sha256,bytes_available:true,metadata:{source_3mf:row.sha256},print_metadata:{},identity_state:'candidate'},'archive'));
    if(!result.items.length)host.textContent='No processed 3MF projects yet. Published originals remain archived independently of geometry.';
    return;
   }
   if(collection==='archive'){await renderChronology(host);return;}
   delete host.dataset.layout;

   if(['history','unprinted','discovered'].includes(collection)){
    const page=await api('/api/fabrication/'+collection);
    host.replaceChildren();
    for(const item of page.items)host.append(card({...item,archive:true,filename:item.filename||item.archived_filename||'Unknown artifact',source_present:item.bytes_available},'archive'));
    if(!page.items.length)host.textContent=collection==='history'?'No observed executions yet. Unknown past results are never invented.':'No archived unprinted artifacts. Preserve local files to retain their original bytes.';
    text('model-count',page.items.length+' archived records');return;
   }
   const listing=await api("/api/printer/sdcard");
   const native=await api("/api/native-sd").catch(()=>({entries:[],assets:[]}));
   const local=listing.local_index?.files||[];const printer=(listing.entries||[]).filter(e=>e.kind==="file");
   host.replaceChildren();const seen=new Set();
   for(const item of local){const key=item.relative_path;if(!key||seen.has(key))continue;seen.add(key);host.append(card(item,"local"));}
   for(const item of printer){if(!item.name)continue;if((native.entries||[]).some(x=>x.name===item.name))continue;host.append(card(item,"printer"));}
   for(const item of native.entries||[])host.append(card({...item,filename:item.original_filename||item.name},"printer_native"));
   for(const asset of native.assets||[])if(asset.cache_available)host.append(card({...asset,name:asset.original_filename,source_present:true},"native_cache"));
   text("model-count",host.childElementCount+" files · "+(listing.local_index?.status||listing.status||"unknown"));
   if(!host.childElementCount){const empty=document.createElement("div");empty.className="tile";empty.textContent=listing.local_index?.error||listing.error||"No indexed G-code available. Configure a read-only source volume or refresh the printer SD listing.";host.append(empty);}
  }catch(e){if(e.message!=="Authentication required"){host.replaceChildren();const error=document.createElement("div");error.className="tile";error.textContent="File listing unavailable: "+e.message;host.append(error);text("model-count","Unavailable");}}
  finally{state.loading=false;filterModels();if(requestedProvider!==$("storage-provider").value||(registryPages[requestedProvider]&&requestedFilter!==$("model-filter").value))loadFiles();}
 }
 $('archive-original').onclick=()=>$('archive-original-file').click();
 $('archive-original-file').onchange=async()=>{const file=$('archive-original-file').files[0];if(!file)return;try{
  if(file.size>50*1024*1024)throw Error('G-code exceeds 50 MiB archive limit');
  const result=await api('/api/fabrication/ingest?filename='+encodeURIComponent(file.name),{method:'POST',headers:{'Content-Type':'application/octet-stream','X-Artifact-Source':'hmi:original-upload'},body:file});
  await openInspector({filename:file.name,sha256:result.sha256,archive:true,bytes_available:true,source_present:true,lifecycle:{archive:'archived',discovery:'uploaded_original',identity:'SD relationship unverified',execution:'not_started'}},'archive');
 }catch(error){text('inspector-status','Archival failed: '+error.message);}finally{$('archive-original-file').value='';}};
 $('associate-execution').onclick=async()=>{const sha=$('execution-artifact').value;const file=inspected?.file;if(!sha||!file||!confirm('Record your association with this exact archived G-code? This is operator supplied, not hardware-verified identity.'))return;try{await api('/api/fabrication/executions/'+encodeURIComponent(file.id)+'/identity',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sha256:sha,transport:$('execution-transport').value})});openInspector({...file,sha256:sha,source_present:true,bytes_available:true,lifecycle:{...file.lifecycle,archive:'archived',identity:'operator_supplied'}},'archive');}catch(error){text('execution-record',error.message);}};
 $('add-observation').onclick=()=>$('observation-file').click();
 $('observation-file').onchange=async()=>{const file=$('observation-file').files[0];if(!file||!inspected?.file?.id)return;const execution=inspected.file;
  try{if(file.size>50*1024*1024)throw Error('Observation exceeds 50 MiB');const uploaded=await api('/api/fabrication/ingest?artifact_kind=observation&filename='+encodeURIComponent(file.name),{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});const kind=/\.(mp4|webm)$/i.test(file.name)?'timelapse':/\.json$/i.test(file.name)?'telemetry':'photo';await api('/api/fabrication/executions/'+encodeURIComponent(execution.id)+'/attachments',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sha256:uploaded.sha256,kind})});text('execution-record','Observation archived and attached; physical quality remains separately assessed');}catch(error){text('execution-record',error.message);}finally{$('observation-file').value='';}
 };
 $('save-quality').addEventListener('click',async()=>{if(!(inspected?.file?.started_at||inspected?.file?.archive_collection==='history'))return;try{const result=await api('/api/fabrication/executions/'+encodeURIComponent(inspected.file.id)+'/quality',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({result:$('quality-result').value,notes:$('quality-notes').value})});text('execution-record','Physical quality '+result.result+' · operator supplied; machine outcome unchanged');}catch(error){text('execution-record',error.message);}});
 $('archive-files').addEventListener('click',async()=>{try{const result=await api('/api/fabrication/reconcile',{method:'POST'});text('model-count',result.archived+' immutable artifacts preserved');}catch(error){text('model-count',error.message);}});
 $('print-again').addEventListener('click',async()=>{if(!inspected?.file?.archive)return;try{
  const file=inspected.file;const endpoint=(file.started_at||file.archive_collection==='history')?'/api/fabrication/executions/'+encodeURIComponent(file.id)+'/stage':'/api/fabrication/artifacts/'+file.sha256+'/stage';
  await api(endpoint+(file.baseline_id&&!file.started_at?'?baseline_id='+file.baseline_id:''),{method:'POST'});closeInspector();route('print-bay');
 }catch(error){text('inspector-status',error.message);}});

 const updateGeometryPresentation=()=>inspectorViewer?.setPresentation($('geometry-mode').value,Number($('geometry-range').value)/100);
 $('geometry-mode').addEventListener('change',updateGeometryPresentation);$('geometry-range').addEventListener('input',updateGeometryPresentation);
 let nativeTransfer=null;
 $("retrieve-sd").addEventListener("click",async()=>{
  if(!inspected||nativeTransfer)return;
  const selected=inspected,epoch=inspectorEpoch;
  try{
   const job=await api('/api/native-sd/transfers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename:selected.file.name})});
   nativeTransfer=job.id;$("retrieve-sd").hidden=true;$("cancel-sd").hidden=false;
   while(nativeTransfer){
    const progress=await api('/api/native-sd/transfers/'+job.id);
    text('native-sd-status',progress.status+' · '+progress.received+' / '+(progress.size??'—')+' bytes');
    if(['complete','failed','cancelled'].includes(progress.status)){
     nativeTransfer=null;$("cancel-sd").hidden=true;
     if(progress.status==='complete'&&epoch===inspectorEpoch)await openInspector({...progress.asset,name:selected.file.name,source_present:true},'native_cache');
     else if(progress.error)text('native-sd-status',progress.error);
     break;
    }
    await new Promise(resolve=>setTimeout(resolve,500));
   }
  }catch(error){nativeTransfer=null;text('native-sd-status',error.message);$("cancel-sd").hidden=true;}
 });
 $("cancel-sd").addEventListener('click',async()=>{if(nativeTransfer)try{await api('/api/native-sd/transfers/'+nativeTransfer,{method:'DELETE'});}catch(error){text('native-sd-status',error.message);}});
 $("copy-sd-local").addEventListener('click',async()=>{
  if(inspected?.source!=='native_cache')return;
  try{const result=await api('/api/native-sd/assets/'+inspected.file.sha256+'/import-local',{method:'POST'});text('native-sd-status',result.status+' · '+result.filename+' · no print selected');}
  catch(error){text('native-sd-status',error.message);}
 });
 $("factory-select-file").addEventListener('click',async()=>{
  const filename=inspected?.file.name;if(!filename||!confirm('I am beside an idle printer. Select '+filename+' only, without starting a print?'))return;
  try{const result=await api('/api/factory-usb/query',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operation:'select',filename,confirmation:'SELECT '+filename,attended:true,printer_idle_confirmed:true})});text('native-sd-status','Selected '+result.data.filename+' · '+result.data.size_bytes+' bytes · no print requested');}
  catch(error){text('native-sd-status',error.message);}
 });
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
   text('sd-import-status',hmiImportEnabled?'Import available':'Import disabled');
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
