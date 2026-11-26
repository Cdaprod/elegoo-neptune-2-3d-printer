import * as THREE from 'three';
import {preparationState} from '/static/print_bay_state.js';
import {OrbitControls} from '/static/vendor/OrbitControls.js';
const $ = id => document.getElementById(id);
const text = (tag, value) => {const e=document.createElement(tag);e.textContent=value;return e;};
let projects=[], artifacts=[], selectedPlate=null, views=[], generation=0, worker={online:false}, jobs=[], renderKey=null, refreshing=false, deliveryInFlight=false;
async function api(path, body) {
  const r=await fetch('/api/manufacturing'+path, body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(!r.ok){let j;try{j=await r.json();}catch{}const error=Error(j?.detail||`Request failed (${r.status})`);error.status=r.status;throw error;}return r.json();
}
function message(value){$('message').textContent=value;}
function project(){return projects.find(p=>p.name===$('projects').value);}
function fileUrl(a,name){return `/api/manufacturing/artifacts/${a.id}/files/${encodeURIComponent(name)}`;}
function mesh(buffer){
  const data=new DataView(buffer), values=[];
  if(buffer.byteLength>=84 && 84+data.getUint32(80,true)*50===buffer.byteLength){
    for(let i=84;i<buffer.byteLength;i+=50)for(let j=12;j<48;j+=4)values.push(data.getFloat32(i+j,true));
  }else{
    const src=new TextDecoder().decode(buffer), re=/\bvertex\s+(\S+)\s+(\S+)\s+(\S+)/g;
    for(const match of src.matchAll(re))values.push(...match.slice(1).map(Number));
  }
  if(!values.length||values.some(v=>!Number.isFinite(v)))throw Error('Invalid preview mesh');
  const g=new THREE.BufferGeometry();g.setAttribute('position',new THREE.Float32BufferAttribute(values,3));g.computeVertexNormals();return g;
}
async function preview(canvas, plate, artifact){
  const currentGeneration=generation;
  if(!artifact&&!project()?.geometryAvailable){canvas.replaceWith(text('p','No exported geometry yet'));return;}
  try{
    const renderer=new THREE.WebGLRenderer({canvas,antialias:true,alpha:true});renderer.setPixelRatio(Math.min(devicePixelRatio,2));
    const scene=new THREE.Scene(), camera=new THREE.PerspectiveCamera(35,1,.1,10000);
    scene.add(new THREE.HemisphereLight(0xc1efff,0x203040,3));const light=new THREE.DirectionalLight(0xffffff,3);light.position.set(80,140,90);scene.add(light);
    const group=new THREE.Group();scene.add(group);
    const geometries=await Promise.all(plate.objects.map(async obj=>{
      const r=await fetch(artifact?fileUrl(artifact,obj.export):`/api/manufacturing/projects/${encodeURIComponent(project().name)}/geometry/${encodeURIComponent(obj.export)}`);if(!r.ok)throw Error('Preview unavailable');return mesh(await r.arrayBuffer());
    }));
    if(currentGeneration!==generation){geometries.forEach(g=>g.dispose());renderer.dispose();return;}
    for(const g of geometries)group.add(new THREE.Mesh(g,new THREE.MeshStandardMaterial({color:0x69bbcc,roughness:.55,metalness:.15})));
    // CAD uses Z-up. Apply one assembly rotation, preserving relative object positions.
    group.rotation.x=-Math.PI/2;const box=new THREE.Box3().setFromObject(group), center=box.getCenter(new THREE.Vector3());group.position.sub(center);
    const size=box.getSize(new THREE.Vector3()).length();camera.position.set(size*.85,size*.65,size);camera.lookAt(0,0,0);
    const controls=new OrbitControls(camera,canvas);controls.enableDamping=true;controls.enablePan=false;
    const view={renderer,controls,scene,camera};views.push(view);
  }catch(e){canvas.replaceWith(text('p',e.message));}
}
function dispose(){generation++;for(const v of views){v.controls.dispose();v.scene.traverse(o=>{o.geometry?.dispose();o.material?.dispose();});v.renderer.dispose();}views=[];}
function stateFor(plate){return preparationState(project(),plate,artifacts,jobs,worker);}
function selected(){return project()?.plates.find(p=>p.name===selectedPlate);}
function renderPlates(){
  const p=project();$('empty').hidden=!!p;$('selected').hidden=!p;$('build-details').hidden=!p;
  if(!p){dispose();$('plates').replaceChildren();renderKey=null;return;}
  $('project-name').textContent=p.name;$('repository').textContent=p.repository;
  const key=JSON.stringify([p.name,p.source,selectedPlate,artifacts.map(a=>[a.id,a.fresh,a.staged])]);
  if(key!==renderKey){
    renderKey=key;dispose();$('plates').replaceChildren();
    for(const plate of p.plates){
      const state=stateFor(plate),a=state.artifact||artifacts.find(a=>a.project===p.name&&a.plate===plate.name);
      const card=text('div','');card.className='plate'+(plate.name===selectedPlate?' selected':'');card.tabIndex=0;card.setAttribute('role','button');card.setAttribute('aria-label','Select '+plate.name);card.dataset.plate=plate.name;
      const canvas=document.createElement('canvas');canvas.setAttribute('aria-label',plate.name+' interactive 3D preview');card.append(canvas,text('h3',plate.name));
      const status=text('span',state.label);status.className='badge';card.append(status);
      let down=null;card.onpointerdown=e=>{down=[e.clientX,e.clientY];};card.onclick=e=>{if(down&&Math.hypot(e.clientX-down[0],e.clientY-down[1])>8)return;if(selectedPlate!==plate.name){selectedPlate=plate.name;renderPlates();}};card.onkeydown=e=>{if(e.key==='Enter')card.click();};$('plates').append(card);preview(canvas,plate,a);
    }
  }
  for(const card of $('plates').children){const plate=p.plates.find(x=>x.name===card.dataset.plate);card.querySelector('.badge').textContent=stateFor(plate).label;}
  renderSelected();
}
function renderSelected(){
  const plate=selected();if(!plate)return;
  const state=stateFor(plate), a=state.artifact;
  $('plate-name').textContent=plate.name;$('output').textContent=plate.output;$('revision').textContent=a?.revision||'';
  $('state').textContent=state.label;$('readiness-dot').className='status-dot '+state.kind;$('preparation-detail').textContent=state.detail;
  $('delivery-actions').hidden=!a;$('retry').hidden=state.kind!=='failed';$('retry').disabled=!worker.online;
  $('deploy').disabled=deliveryInFlight||!worker.delivery?.upload_enabled;$('deploy').title=worker.delivery?.upload_enabled?'Upload this prepared revision without starting a print':'Configure and enable the OctoPrint upload connection';
  $('export-sd').disabled=deliveryInFlight||!worker.delivery?.sd_mounted;$('export-sd').title=worker.delivery?.sd_mounted?'Export this prepared revision':'Insert the configured SD card';
  $('delivery-note').textContent=a?'Uploading does not start a print. SD export preserves existing files.':'Building, validation, slicing, previews and staging run in the background.';
  $('automation-status').textContent=(state.automatic?'Automatic build, slice and stage enabled.':'This project has manual preparation policies.')+' '+(worker.online?'Build worker connected.':'Build worker offline.');
}
async function artifactAction(action){
  const a=stateFor(selected()).artifact;if(!a||deliveryInFlight)return;
  const operation=action==='deploy'?'UPLOAD':'EXPORT';
  if(!confirm(`${operation} ${a.revision} · ${a.plate}?\n${action==='deploy'?'Uploads to OctoPrint without starting a print.':'Writes to the mounted SD card; existing files are preserved.'}`))return;
  deliveryInFlight=true;renderSelected();
  try{
    const result=await api(`/artifacts/${a.id}/${action}`,{confirmation:`${operation} ${a.id}`});
    message(action==='deploy'?'Uploaded to OctoPrint. No print was started.':`SD export ${result.status==='already_present'?'already verified':'complete'}: ${result.filename}`);
  }catch(e){message(e.message);}finally{deliveryInFlight=false;await refresh();}
}
function renderArtifacts(){
  const table=document.createElement('table'),head=document.createElement('tr');['Revision / Plate','State','Files'].forEach(x=>head.append(text('th',x)));table.append(head);
  for(const a of artifacts.filter(a=>a.project===project()?.name)){
    const tr=document.createElement('tr'),info=document.createElement('td');info.append(text('strong',`${a.revision} · ${a.plate}`),document.createElement('br'),text('code',a.checksum));
    const preview=a.receipt.previews;if(preview){info.append(document.createElement('br'),text('small',`${preview.embedded?'PNG/JPEG preview':'No embedded preview'} · TFT: ${preview.tft}`));}
    tr.append(info,text('td',a.fresh?(a.staged?'Prepared':'Not staged'):'Superseded'));
    const files=document.createElement('td'),link=text('a','Download G-code');link.href=fileUrl(a,a.receipt.output);link.download=a.receipt.output;files.append(link);tr.append(files);table.append(tr);
  }
  $('artifacts').replaceChildren(table);if(!artifacts.length)$('artifacts').append(text('p','No prepared revisions yet.'));
}
async function refresh(){
  if(refreshing)return;refreshing=true;
  try{
    const [ps,as,js,ws]=await Promise.all([api('/projects'),api('/artifacts'),api('/jobs'),api('/worker')]);projects=ps.filter(p=>!p.error);artifacts=as;jobs=js;worker=ws;$('login').hidden=true;
    const prior=$('projects').value;$('projects').replaceChildren(...projects.map(p=>{const o=text('option',p.name);o.value=p.name;return o;}));if(projects.some(p=>p.name===prior))$('projects').value=prior;
    if(!selectedPlate||!project()?.plates.some(p=>p.name===selectedPlate))selectedPlate=project()?.plates[0]?.name;
    renderPlates();renderArtifacts();$('jobs').replaceChildren(...js.map(j=>{let r={};try{r=JSON.parse(j.result||'{}');}catch{}return text('p',`${j.plate} · ${r.phase||j.status}${r.error?' — '+r.error:''}`);}));
    if(!js.length)$('jobs').append(text('p','No preparation jobs recorded.'));
    const invalid=ps.filter(p=>p.error);if(invalid.length)message(invalid.map(p=>p.name+': '+p.error).join('\n'));
  }catch(e){if(e.status===401)$('login').hidden=false;message(e.message);}finally{refreshing=false;}
}
async function queue(action){
  try{await api(`/projects/${encodeURIComponent(project().name)}/jobs`,{plate:selectedPlate,action});message('Preparation queued. This plate will become ready automatically.');await refresh();}catch(e){message(e.message);}
}
$('login').onsubmit=async e=>{e.preventDefault();try{const r=await fetch('/api/auth/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:$('token').value})});if(!r.ok)throw Error('Sign-in failed');$('token').value='';$('login').hidden=true;message('Signed in');await refresh();}catch(e){message(e.message);}};
$('projects').onchange=()=>{selectedPlate=project()?.plates[0]?.name;renderPlates();renderArtifacts();};$('refresh').onclick=refresh;
$('retry').onclick=()=>queue('build-slice-stage');$('deploy').onclick=()=>artifactAction('deploy');$('export-sd').onclick=()=>artifactAction('export-sd');
for(const button of document.querySelectorAll('[data-action]'))button.onclick=()=>queue(button.dataset.action);
$('inventory-panel').addEventListener('toggle',async e=>{if(e.target.open)try{const rows=await api('/inventory');$('inventory').replaceChildren(...rows.map(r=>text('p',`${r.root}/${r.file} · ${r.status}`)));if(!rows.length)$('inventory').textContent='No inventory roots configured or no candidate files found.';}catch(err){message(err.message);}});
function animate(){requestAnimationFrame(animate);for(const v of views){const w=v.renderer.domElement.clientWidth,h=v.renderer.domElement.clientHeight;if(v.renderer.domElement.width!==w*Math.min(devicePixelRatio,2))v.renderer.setSize(w,h,false);v.camera.aspect=w/h;v.camera.updateProjectionMatrix();v.controls.update();v.renderer.render(v.scene,v.camera);}}animate();refresh();setInterval(refresh,3000);
