import * as THREE from 'three';
import {OrbitControls} from '/static/vendor/OrbitControls.js';
const $ = id => document.getElementById(id);
const text = (tag, value) => {const e=document.createElement(tag);e.textContent=value;return e;};
let projects=[], artifacts=[], selectedPlate=null, views=[], generation=0;
async function api(path, body) {
  const r=await fetch('/api/manufacturing'+path, body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(!r.ok){let j;try{j=await r.json();}catch{}throw Error(j?.detail||`Request failed (${r.status})`);}return r.json();
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
function renderPlates(){
  dispose();$('plates').replaceChildren();const p=project();$('empty').hidden=!!p;$('selected').hidden=!p;
  if(!p)return;$('project-name').textContent=p.name;$('repository').textContent=p.repository;
  for(const plate of p.plates){
    const a=artifacts.find(a=>a.project===p.name&&a.plate===plate.name), card=text('div','');card.className='plate'+(plate.name===selectedPlate?' selected':'');card.tabIndex=0;card.setAttribute('role','button');card.setAttribute('aria-label','Select '+plate.name);
    const canvas=document.createElement('canvas');canvas.setAttribute('aria-label',plate.name+' interactive 3D preview');card.append(canvas,text('h3',plate.name));
    const status=text('span',a?(a.fresh?(a.staged?'Staged':'Ready'):'Source changed'):(p.geometryAvailable?'Geometry validated':'Build required'));status.className='badge';card.append(status);
    card.onclick=()=>{if(selectedPlate!==plate.name){selectedPlate=plate.name;renderPlates();}};card.onkeydown=e=>{if(e.key==='Enter')card.click();};$('plates').append(card);preview(canvas,plate,a);
  }
  const plate=p.plates.find(x=>x.name===selectedPlate)||p.plates[0];selectedPlate=plate.name;
  const a=artifacts.find(x=>x.project===p.name&&x.plate===plate.name);
  $('plate-name').textContent=plate.name;$('output').textContent=plate.output;$('revision').textContent=a?.revision||'Not built';$('state').textContent=a?(a.fresh?(a.staged?'Staged':'Ready to stage'):'Source changed'):(p.geometryAvailable?'Geometry validated':'Build required');
}
async function artifactAction(a,action){
  try{
    let body={};
    if(action==='deploy'||action==='export-sd'){
      const operation=action==='deploy'?'UPLOAD':'EXPORT';
      if(!confirm(`${operation} ${a.revision} · ${a.plate}?\nSHA-256: ${a.checksum}\n${action==='deploy'?'Uploads to OctoPrint without starting a print.':'Writes to the mounted NEPTUNE SD card; existing files are preserved.'}`))return;
      body={confirmation:`${operation} ${a.id}`};
    }
    const result=await api(`/artifacts/${a.id}/${action}`,body);message(JSON.stringify(result,null,2));await refresh();
  }catch(e){message(e.message);}
}
function renderArtifacts(){
  const table=document.createElement('table'),head=document.createElement('tr');['Revision / Plate','State','Actions'].forEach(x=>head.append(text('th',x)));table.append(head);
  for(const a of artifacts.filter(a=>a.project===project()?.name)){
    const tr=document.createElement('tr'),info=document.createElement('td');info.append(text('strong',`${a.revision} · ${a.plate}`),document.createElement('br'),text('code',a.checksum.slice(0,16)+'…'));const preview=a.receipt.previews;if(preview){info.append(document.createElement('br'),text('small',`${preview.embedded?'PNG/JPEG preview':'No embedded preview'} · TFT: ${preview.tft}`));}tr.append(info,text('td',a.fresh?(a.staged?'Staged':'Ready'):'Stale'));
    const actions=document.createElement('td');actions.className='row';
    for(const [label,action] of [['Stage','stage'],['Deploy to Printer','deploy'],['Export to SD Card','export-sd']]){const b=text('button',label);b.disabled=!a.fresh||(action!=='stage'&&!a.staged);b.onclick=()=>artifactAction(a,action);actions.append(b);}
    const link=text('a','G-code ↓');link.href=fileUrl(a,a.receipt.output);link.download=a.receipt.output;actions.append(link);tr.append(actions);table.append(tr);
  }
  $('artifacts').replaceChildren(table);if(!artifacts.length)$('artifacts').append(text('p','No artifacts yet. Build and slice a plate to create the first revision.'));
}
async function refresh(){
  try{
    const [ps,as,js]=await Promise.all([api('/projects'),api('/artifacts'),api('/jobs')]);projects=ps.filter(p=>!p.error);artifacts=as;$('login').hidden=true;
    const prior=$('projects').value;$('projects').replaceChildren(...projects.map(p=>{const o=text('option',p.name);o.value=p.name;return o;}));if(projects.some(p=>p.name===prior))$('projects').value=prior;
    if(!selectedPlate||!project()?.plates.some(p=>p.name===selectedPlate))selectedPlate=project()?.plates[0]?.name;
    renderPlates();renderArtifacts();$('jobs').replaceChildren(...js.map(j=>text('p',`${j.plate} · ${j.action} · ${j.status}${j.result?' — '+j.result:''}`)));
    if(!js.length)$('jobs').append(text('p','No queued builds.'));
    const invalid=ps.filter(p=>p.error);if(invalid.length)message(invalid.map(p=>p.name+': '+p.error).join('\n'));
  }catch(e){message(e.message);}
}
$('login').onsubmit=async e=>{e.preventDefault();try{const r=await fetch('/api/auth/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:$('token').value})});if(!r.ok)throw Error('Sign-in failed');$('token').value='';$('login').hidden=true;message('Signed in');await refresh();}catch(e){message(e.message);}};
$('projects').onchange=()=>{selectedPlate=null;selectedPlate=project()?.plates[0]?.name;renderPlates();renderArtifacts();};$('refresh').onclick=refresh;
for(const button of document.querySelectorAll('[data-action]'))button.onclick=async()=>{button.disabled=true;try{const r=await api(`/projects/${encodeURIComponent(project().name)}/jobs`,{plate:selectedPlate,action:button.dataset.action});message(`Job ${r.job} queued. The build worker will process it.`);await refresh();}catch(e){message(e.message);}finally{button.disabled=false;}};
document.querySelector('details').addEventListener('toggle',async e=>{if(e.target.open)try{const rows=await api('/inventory');$('inventory').replaceChildren(...rows.map(r=>text('p',`${r.root}/${r.file} · ${r.status}`)));if(!rows.length)$('inventory').textContent='No inventory roots configured or no candidate files found.';}catch(err){message(err.message);}});
function animate(){requestAnimationFrame(animate);for(const v of views){const w=v.renderer.domElement.clientWidth,h=v.renderer.domElement.clientHeight;if(v.renderer.domElement.width!==w*Math.min(devicePixelRatio,2))v.renderer.setSize(w,h,false);v.camera.aspect=w/h;v.camera.updateProjectionMatrix();v.controls.update();v.renderer.render(v.scene,v.camera);}}animate();refresh();
setInterval(async()=>{try{const jobs=await api('/jobs');if(jobs.some(j=>j.status==='running'||j.status==='queued')){$('jobs').replaceChildren(...jobs.map(j=>text('p',`${j.plate} · ${j.action} · ${j.status}`)));}else if(jobs.length&&jobs[0].id!==window.lastFinishedJob){window.lastFinishedJob=jobs[0].id;await refresh();}}catch{}},3000);
