/* Decorative schematic; camera motion never represents commanded or observed axes. */
import * as THREE from '/static/vendor/three.module.js';
import {makeMachine} from '/static/digital_twin.js';
const host=document.getElementById('hmi-ambience');
try{
 const renderer=new THREE.WebGLRenderer({antialias:false,alpha:true,powerPreference:'low-power'});
 renderer.setPixelRatio(Math.min(devicePixelRatio,1));host.append(renderer.domElement);
 const scene=new THREE.Scene();scene.fog=new THREE.FogExp2(0x08111e,.0009);
 scene.add(new THREE.HemisphereLight(0x8bdcff,0x07111e,2));
 const light=new THREE.DirectionalLight(0x8ee8ff,3);light.position.set(200,500,250);scene.add(light);
 const rim=new THREE.DirectionalLight(0x667cff,2.5);rim.position.set(-300,300,-250);scene.add(rim);
 const machine=makeMachine(scene);machine.setMachinePosition(110,110,85);
 const grid=new THREE.GridHelper(1600,32,0x338fa5,0x17354e);grid.material.transparent=true;grid.material.opacity=.22;scene.add(grid);
 const stars=new Float32Array(100*3);for(let i=0;i<100;i++){stars[i*3]=Math.sin(i*127.1)*650;stars[i*3+1]=35+(i*73%550);stars[i*3+2]=Math.cos(i*311.7)*650;}
 const dustGeometry=new THREE.BufferGeometry();dustGeometry.setAttribute('position',new THREE.BufferAttribute(stars,3));
 const dust=new THREE.Points(dustGeometry,new THREE.PointsMaterial({color:0x86d9f1,size:1.4,transparent:true,opacity:.35,depthWrite:false}));scene.add(dust);
 const camera=new THREE.PerspectiveCamera(38,1,1,3000),focus=new THREE.Vector3(25,165,0);
 const positions=new THREE.CatmullRomCurve3([[440,260,400],[140,180,245],[-320,245,240],[-230,560,-290],[360,390,-280]].map(p=>new THREE.Vector3(...p)),true,'catmullrom',.3);
 const targets=new THREE.CatmullRomCurve3([[25,165,0],[0,125,0],[0,210,-20],[0,300,-10],[35,180,0]].map(p=>new THREE.Vector3(...p)),true,'catmullrom',.3);
 const reduce=matchMedia('(prefers-reduced-motion: reduce)');
 let last=0,phase=0,journey=0,blend=0,target=0,frame=0,disposed=false,painted=false;
 addEventListener('hmi:cinematic',e=>{target=e.detail.active?1:0;painted=false;});
 reduce.addEventListener('change',()=>{painted=false;});addEventListener('resize',()=>{painted=false;});
 const ambient=new THREE.Vector3(),cinematic=new THREE.Vector3(),look=new THREE.Vector3();
 function draw(now){
  if(disposed)return;frame=requestAnimationFrame(draw);
  if(document.hidden||(reduce.matches&&painted)||now-last<1000/20)return;
  const delta=last?Math.min(now-last,100):0;last=now;
  if(!reduce.matches){phase+=delta*.000025;journey=(journey+delta/100000)%1;blend+=(target-blend)*(1-Math.exp(-delta/(target?750:350)));}else blend=target;
  const w=host.clientWidth,h=host.clientHeight;if(!w||!h)return;
  if(renderer.domElement.width!==w||renderer.domElement.height!==h)renderer.setSize(w,h,false);
  camera.aspect=w/h;camera.fov=38-blend*5;camera.updateProjectionMatrix();
  ambient.set(focus.x+Math.sin(.65+phase)*430,260+Math.sin(phase*.4)*20,Math.cos(.65+phase)*430);
  positions.getPointAt(reduce.matches ? .08 : journey,cinematic);camera.position.copy(ambient).lerp(cinematic,blend);
  targets.getPointAt(reduce.matches ? .08 : journey,look);look.lerp(focus,1-blend);camera.lookAt(look);
  light.intensity=3+blend*1.4;rim.intensity=2.5+blend;dust.material.opacity=.1+blend*.3;
  dust.rotation.y=reduce.matches?0:phase*.12;
  const caption=document.getElementById('cinema-shot');if(caption)caption.textContent=['The architecture of motion','Where layers take shape','Precision in every axis','A study in structure','From motion to matter'][Math.floor(journey*5)];
  renderer.render(scene,camera);painted=true;
 }
 frame=requestAnimationFrame(draw);host.dataset.renderer='ready';
 addEventListener('pagehide',event=>{if(event.persisted)return;disposed=true;cancelAnimationFrame(frame);scene.traverse(o=>{o.geometry?.dispose();if(Array.isArray(o.material))o.material.forEach(m=>m.dispose());else o.material?.dispose();});renderer.dispose();});
}catch{host.dataset.renderer='unavailable';}
