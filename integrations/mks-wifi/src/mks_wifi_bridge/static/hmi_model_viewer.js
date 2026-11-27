/* On-demand G-code inspection: native-quality silhouette without idle GPU redraws. */
import * as THREE from "/static/vendor/three.module.js";
import { OrbitControls } from "/static/vendor/OrbitControls.js";

const MATERIALS = [0xa6bdcc, 0x6899b7, 0xd8a76c, 0xd0e1e8];

export function mountViewer(host, model) {
  const segments = Array.isArray(model.segments) ? model.segments : [];
  if (!segments.length || !Array.isArray(model.bounds)) throw new Error("No printable geometry available");
  const lo = model.bounds[0], hi = model.bounds[1];
  const center = [(lo[0]+hi[0])/2, (lo[1]+hi[1])/2, (lo[2]+hi[2])/2];
  const span = Math.max(15, hi[0]-lo[0], hi[1]-lo[1], hi[2]-lo[2]);
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0a1928);
  scene.add(new THREE.HemisphereLight(0xe6f7ff, 0x213647, 2.1));
  const key = new THREE.DirectionalLight(0xfff2dc, 3);
  key.position.set(span*1.2, span*1.7, span);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0x7bbfff, 1.2);
  fill.position.set(-span, span*.6, -span);
  scene.add(fill);
  const grid = new THREE.GridHelper(span*2.2, 12, 0x4b7f9d, 0x28465d);
  grid.position.y = lo[2]-center[2]-1.5;
  scene.add(grid);

  // Small previews can supersample; large toolpaths must preserve Pi 4 GPU headroom.
  const renderer = new THREE.WebGLRenderer({antialias:true, alpha:false, powerPreference:"high-performance"});
  const dpr = Math.min(1.5, (window.devicePixelRatio || 1) * (segments.length > 20000 ? 1 : segments.length > 6000 ? 1.15 : 1.3));
  renderer.setPixelRatio(dpr);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.domElement.style.width = "100%";
  renderer.domElement.style.height = "100%";
  renderer.domElement.style.display = "block";
  renderer.domElement.style.touchAction = "none";
  renderer.domElement.setAttribute("aria-label", "Interactive reconstructed G-code extrusion model");
  host.replaceChildren(renderer.domElement);
  host.dataset.viewerPixelRatio = dpr.toFixed(2);
  const camera = new THREE.OrthographicCamera(-span,span,span,-span,0.1,span*25);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.12;
  controls.enablePan = false;
  controls.minZoom = 0.45;
  controls.maxZoom = 12;
  controls.target.set(0,0,0);

  const cylinder = new THREE.CylinderGeometry(1,1,1,10);
  const origin = new THREE.Vector3(0,1,0);
  const start = new THREE.Vector3(), end = new THREE.Vector3();
  const mid = new THREE.Vector3(), direction = new THREE.Vector3(), quaternion = new THREE.Quaternion();
  const matrix = new THREE.Matrix4(), scale = new THREE.Vector3();
  const meshes = [];
  const groups = [[],[],[],[]];
  for (const segment of segments) {
    const category = segment[7];
    if (Number.isInteger(category) && category>=0 && category<groups.length) groups[category].push(segment);
  }
  for (let category=0;category<groups.length;category++) {
    const group = groups[category];
    if (!group.length) continue;
    const material = new THREE.MeshStandardMaterial({
      color:MATERIALS[category], roughness:.58, metalness:.11
    });
    const mesh = new THREE.InstancedMesh(cylinder,material,group.length);
    mesh.frustumCulled = false;
    group.forEach((s,i)=>{
      start.set(s[0]-center[0],s[2]-center[2],-(s[1]-center[1]));
      end.set(s[3]-center[0],s[5]-center[2],-(s[4]-center[1]));
      mid.copy(start).add(end).multiplyScalar(.5);
      direction.copy(end).sub(start);
      const length = direction.length();
      quaternion.setFromUnitVectors(origin, direction.normalize());
      const radius = Math.max(.13, Math.min(1.2, Number(s[6])*.5 || .22));
      scale.set(radius,length,radius);
      matrix.compose(mid,quaternion,scale);
      mesh.setMatrixAt(i,matrix);
    });
    mesh.instanceMatrix.needsUpdate=true;
    scene.add(mesh);
    meshes.push(mesh);
  }

  let resolveReady,rejectReady;
  const ready=new Promise((resolve,reject)=>{resolveReady=resolve;rejectReady=reject;});
  let disposed=false,frame=0,interacting=false,drawing=false,settleUntil=0,renderFrames=0;
  function schedule() {
    if(!disposed&&!frame&&!document.hidden)frame=requestAnimationFrame(draw);
  }
  function invalidate() {
    settleUntil=Math.max(settleUntil,performance.now()+450);
    schedule();
  }
  function onControlsChange() {
    // OrbitControls.update emits 'change' inside draw(); avoid duplicate RAF loops.
    if(!drawing)invalidate();
  }
  function onControlsStart() {interacting=true;invalidate();}
  function onControlsEnd() {interacting=false;invalidate();}
  controls.addEventListener("change",onControlsChange);
  controls.addEventListener("start",onControlsStart);
  controls.addEventListener("end",onControlsEnd);

  function setView(name) {
    if (name==="top") camera.position.set(0,span*2.7,.01);
    else if (name==="front") camera.position.set(0,span*.15,span*2.6);
    else camera.position.set(span*1.8,span*1.35,span*1.9);
    camera.lookAt(controls.target);
    controls.update();
    invalidate();
  }
  let width=0,height=0;
  function resize() {
    if(disposed)return;
    const w=Math.max(1,host.clientWidth), h=Math.max(1,host.clientHeight);
    if(w!==width||h!==height){
      width=w;height=h;
      renderer.setSize(w,h,false);
      const aspect=w/h;
      camera.left=-span*1.05*aspect;
      camera.right=span*1.05*aspect;
      camera.top=span*1.05;
      camera.bottom=-span*1.05;
      camera.updateProjectionMatrix();
      invalidate();
    }
  }
  function draw(now) {
    frame=0;
    if(disposed||document.hidden||!host.isConnected)return;
    drawing=true;
    controls.update();
    drawing=false;
    try{renderer.render(scene,camera);resolveReady(true);}
    catch(error){rejectReady(error);return;}
    host.dataset.viewerFrames=String(++renderFrames);
    if(interacting||now<settleUntil)schedule();
  }
  function visibility() {if(!document.hidden)invalidate();}
  function contextLost(event) {event.preventDefault();host.dataset.viewerStatus="context-lost";}
  renderer.domElement.addEventListener("webglcontextlost",contextLost);
  document.addEventListener("visibilitychange",visibility);
  const observer = new ResizeObserver(resize);
  observer.observe(host);
  setView("iso");
  resize();
  return {
    ready,setView,
    dispose() {
      if(disposed)return;
      disposed=true;resolveReady(false);
      cancelAnimationFrame(frame);
      observer.disconnect();
      document.removeEventListener("visibilitychange",visibility);
      renderer.domElement.removeEventListener("webglcontextlost",contextLost);
      controls.removeEventListener("change",onControlsChange);
      controls.removeEventListener("start",onControlsStart);
      controls.removeEventListener("end",onControlsEnd);
      controls.dispose();
      cylinder.dispose();
      grid.geometry.dispose();
      grid.material.dispose();
      for(const mesh of meshes)mesh.material.dispose();
      renderer.dispose();
      renderer.forceContextLoss();
      renderer.domElement.remove();
      delete host.dataset.viewerPixelRatio;
      delete host.dataset.viewerFrames;
      delete host.dataset.viewerStatus;
    }
  };
}
