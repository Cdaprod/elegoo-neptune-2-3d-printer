/* On-demand G-code inspection: native-quality silhouette without idle GPU redraws. */
import * as THREE from "/static/vendor/three.module.js";
import { OrbitControls } from "/static/vendor/OrbitControls.js";

const MATERIALS = [0xa6bdcc, 0x6899b7, 0xd8a76c, 0xd0e1e8];

export function mountViewer(host, model) {
  const segments = Array.isArray(model.segments) ? model.segments : [];
  const surface=Array.isArray(model.surface_vertices)?model.surface_vertices:null;
  if ((!segments.length&&!surface?.length) || !Array.isArray(model.bounds)) throw new Error("No printable geometry available");
  const frameBounds=model.display_bounds||model.bounds;
  const lo = frameBounds[0], hi = frameBounds[1];
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
  renderer.domElement.setAttribute("aria-label", surface?"Interactive original STL surface":"Interactive reconstructed G-code extrusion model");
  host.replaceChildren(renderer.domElement);
  host.dataset.viewerPixelRatio = dpr.toFixed(2);
  const camera = new THREE.OrthographicCamera(-span,span,span,-span,0.1,span*25);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.12;
  controls.enablePan = false;
  controls.minZoom = 0.05;
  controls.maxZoom = 12;
  controls.target.set(0,0,0);

  const cylinder = new THREE.CylinderGeometry(1,1,1,10);
  const origin = new THREE.Vector3(0,1,0);
  const start = new THREE.Vector3(), end = new THREE.Vector3();
  const mid = new THREE.Vector3(), direction = new THREE.Vector3(), quaternion = new THREE.Quaternion();
  const matrix = new THREE.Matrix4(), scale = new THREE.Vector3();
  const meshes = [];
  if(surface){
    const values=new Float32Array(surface.length);
    for(let i=0;i<surface.length;i+=3){values[i]=surface[i]-center[0];values[i+1]=surface[i+2]-center[2];values[i+2]=-(surface[i+1]-center[1]);}
    const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.BufferAttribute(values,3));geometry.computeVertexNormals();
    const mesh=new THREE.Mesh(geometry,new THREE.MeshStandardMaterial({color:0x8ab4c7,roughness:.48,metalness:.15,side:THREE.DoubleSide}));scene.add(mesh);meshes.push(mesh);
  }
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

  // One immutable line buffer, shared by all toolpath inspection ranges.
  const pathPositions=new Float32Array(segments.length*6);
  segments.forEach((s,i)=>{pathPositions.set([s[0]-center[0],s[2]-center[2],-(s[1]-center[1]),s[3]-center[0],s[5]-center[2],-(s[4]-center[1])],i*6);});
  const pathGeometry=new THREE.BufferGeometry();pathGeometry.setAttribute('position',new THREE.BufferAttribute(pathPositions,3));
  const paths=new THREE.LineSegments(pathGeometry,new THREE.LineBasicMaterial({color:0x6cdbed}));paths.visible=false;scene.add(paths);
  const section=new THREE.Plane(new THREE.Vector3(0,-1,0),0);
  function setPresentation(mode='shaded',fraction=1){
    if(surface)mode='shaded';fraction=Math.max(0,Math.min(1,Number(fraction)||0));
    const toolpath=mode==='toolpath';for(const mesh of meshes)mesh.visible=!toolpath;
    paths.visible=toolpath;pathGeometry.setDrawRange(0,Math.max(0,Math.round(segments.length*fraction))*2);
    section.constant=lo[2]-center[2]+(hi[2]-lo[2])*fraction;
    renderer.clippingPlanes=mode==='layers'?[section]:[];
    host.dataset.viewerPresentation=mode;host.dataset.viewerRange=String(fraction);invalidate();
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

  let fitSpan=span;
  function setView(name) {
    const frame=name==='all'?model.bounds:frameBounds;
    fitSpan=Math.max(15,...frame[1].map((v,i)=>v-frame[0][i]));
    controls.target.set((frame[0][0]+frame[1][0])/2-center[0],
      (frame[0][2]+frame[1][2])/2-center[2],-((frame[0][1]+frame[1][1])/2-center[1]));
    const offset=name==='top'?new THREE.Vector3(0,fitSpan*2.7,.01):name==='front'?
      new THREE.Vector3(0,fitSpan*.15,fitSpan*2.6):new THREE.Vector3(fitSpan*1.8,fitSpan*1.35,fitSpan*1.9);
    camera.position.copy(controls.target).add(offset);camera.zoom=1;camera.far=fitSpan*25;
    camera.lookAt(controls.target);resize(true);controls.update();invalidate();
    host.dataset.viewerFraming=name==='all'?'all_paths':'model';
  }
  let width=0,height=0;
  function resize(force=false) {
    if(disposed)return;
    const w=Math.max(1,host.clientWidth), h=Math.max(1,host.clientHeight);
    if(force||w!==width||h!==height){
      width=w;height=h;
      renderer.setSize(w,h,false);
      const aspect=w/h;
      camera.left=-fitSpan*1.05*aspect;
      camera.right=fitSpan*1.05*aspect;
      camera.top=fitSpan*1.05;
      camera.bottom=-fitSpan*1.05;
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
  const observer = new ResizeObserver(()=>resize());
  observer.observe(host);
  setView("iso");
  resize();
  return {
    ready,setView,setPresentation,
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
      for(const geometry of new Set([cylinder,...meshes.map(mesh=>mesh.geometry)]))geometry.dispose();
      grid.geometry.dispose();
      grid.material.dispose();
      for(const mesh of meshes){mesh.material.dispose();if(mesh.isInstancedMesh)mesh.dispose();}
      pathGeometry.dispose();paths.material.dispose();
      renderer.dispose();
      renderer.forceContextLoss();
      renderer.domElement.remove();
      delete host.dataset.viewerPixelRatio;delete host.dataset.viewerFraming;
      delete host.dataset.viewerFrames;
      delete host.dataset.viewerStatus;delete host.dataset.viewerPresentation;delete host.dataset.viewerRange;
    }
  };
}
