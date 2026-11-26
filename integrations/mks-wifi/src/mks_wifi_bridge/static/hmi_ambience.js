/* Decorative schematic; never used as evidence of actual printer position. */
import * as THREE from '/static/vendor/three.module.js';
import {makeMachine} from '/static/digital_twin.js';
const host=document.getElementById('hmi-ambience');
try{
 const renderer=new THREE.WebGLRenderer({antialias:false,alpha:true,powerPreference:'low-power'});
 renderer.setPixelRatio(Math.min(devicePixelRatio,1));host.append(renderer.domElement);
 const scene=new THREE.Scene();scene.add(new THREE.HemisphereLight(0x73c9ff,0x07111e,2));
 const light=new THREE.DirectionalLight(0x8ee8ff,3);light.position.set(200,500,250);scene.add(light);
 const machine=makeMachine(scene);machine.setMachinePosition(110,110,85);
 const camera=new THREE.PerspectiveCamera(38,1,1,3000),focus=new THREE.Vector3(25,165,0);
 const reduce=matchMedia('(prefers-reduced-motion: reduce)');
 let last=0,phase=0,frame=0,disposed=false,painted=false;
 reduce.addEventListener("change",()=>{painted=false;});
 addEventListener("resize",()=>{painted=false;});
 function draw(now){
  if(disposed)return;frame=requestAnimationFrame(draw);
  if(document.hidden||(reduce.matches&&painted)||now-last<1000/20)return;
  const delta=last?Math.min(now-last,100):0;last=now;if(!reduce.matches)phase+=delta*.000035;
  const w=host.clientWidth,h=host.clientHeight;if(!w||!h)return;
  if(renderer.domElement.width!==w||renderer.domElement.height!==h)renderer.setSize(w,h,false);
  camera.aspect=w/h;camera.updateProjectionMatrix();
  camera.position.set(focus.x+Math.sin(.65+phase)*430,260+Math.sin(phase*.4)*20,Math.cos(.65+phase)*430);
  camera.lookAt(focus);renderer.render(scene,camera);painted=true;
 }
 frame=requestAnimationFrame(draw);host.dataset.renderer='ready';
 addEventListener('pagehide',()=>{disposed=true;cancelAnimationFrame(frame);scene.traverse(o=>{o.geometry?.dispose();if(Array.isArray(o.material))o.material.forEach(m=>m.dispose());else o.material?.dispose();});renderer.dispose();},{once:true});
}catch{host.dataset.renderer='unavailable';}
