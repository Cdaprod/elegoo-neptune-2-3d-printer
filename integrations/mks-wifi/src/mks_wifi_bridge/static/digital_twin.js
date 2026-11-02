import * as THREE from "/static/vendor/three.module.js";
import { OrbitControls } from "/static/vendor/OrbitControls.js";
import { NEPTUNE_2_MACHINE } from "/static/neptune_machine.js";
import { parseToolpath } from "/static/gcode_toolpath.js";

const instances = [];
const el = (id) => document.getElementById(id);
const BUILD = NEPTUNE_2_MACHINE.buildVolumeMm;
const SURFACE_Y = 38;
const RADIAL_STEPS = 8;
const INDEXES_PER_BEAD = RADIAL_STEPS * 6;
let data = null;
let mode = "live";
let program = null;
let replaySeconds = 0;
let historyOffsetMs = 0;
let playing = false;
let speed = 1;
let selectedLayer = "all";
let historyPlayback = null;
let historyModulePromise = null;
let historyProgressAtOffset = null;
let sharedRender = null;

function addBox(parent, x, y, z, sx, sy, sz, color, material = {}) {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(sx, sy, sz), new THREE.MeshStandardMaterial({ color, ...material }));
  mesh.position.set(x, y, z);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  parent.add(mesh);
  return mesh;
}

function toBedLocal(point) {
  return new THREE.Vector3(
    point.x - BUILD.x / 2,
    SURFACE_Y + point.z,
    BUILD.y / 2 - point.y,
  );
}

function makeHomeSwitch(parent, x, y, z, label) {
  const switchBody = addBox(parent, x, y, z, 9, 8, 7, 0xff667b, { metalness: .18, roughness: .35 });
  switchBody.userData.homeSwitch = label;
  return switchBody;
}

function makeMachine(scene) {
  const root = new THREE.Group();
  scene.add(root);
  const frame = new THREE.Group();
  root.add(frame);
  const dark = 0x26394d;
  const extrusion = 0x91a9bf;
  const outerWidth = 420;
  const outerDepth = 414;
  const outerHeight = 470;
  // Base and upright frame are proportioned to the Neptune 2 envelope. The
  // exact extrusion lengths and switch mounting locations remain schematic.
  for (const x of [-outerWidth / 2, outerWidth / 2]) {
    addBox(frame, x, 22, 0, 20, 34, outerDepth, dark, { metalness: .55, roughness: .38 });
    addBox(frame, x, outerHeight / 2 + 20, -20, 20, outerHeight, 18, extrusion, { metalness: .68, roughness: .3 });
    addBox(frame, x, 230, 20, 20, 420, 18, dark, { metalness: .58, roughness: .34 });
  }
  for (const z of [-outerDepth / 2, outerDepth / 2]) {
    addBox(frame, 0, 22, z, outerWidth, 34, 20, dark, { metalness: .55, roughness: .38 });
  }
  addBox(frame, 0, outerHeight + 20, -20, outerWidth, 18, 18, dark, { metalness: .65, roughness: .32 });
  addBox(frame, 0, 22, 0, outerWidth - 20, 8, outerDepth - 20, 0x182535);
  // Z screws / guide rails on either side of the X gantry.
  for (const x of [-165, 165]) {
    const screw = new THREE.Mesh(
      new THREE.CylinderGeometry(2.2, 2.2, 360, 12),
      new THREE.MeshStandardMaterial({ color: 0xa4b4c4, metalness: .82, roughness: .22 }),
    );
    screw.position.set(x, 295, 0);
    frame.add(screw);
    addBox(frame, x + (x < 0 ? 10 : -10), 290, 0, 7, 360, 9, 0x667f98, { metalness: .62, roughness: .35 });
  }
  // Build envelope frame uses the published 220 × 220 × 250 mm print volume.
  const volume = new THREE.LineSegments(
    new THREE.EdgesGeometry(new THREE.BoxGeometry(BUILD.x, BUILD.z, BUILD.y)),
    new THREE.LineBasicMaterial({ color: 0x478fc4, transparent: true, opacity: .36 }),
  );
  volume.position.set(0, SURFACE_Y + BUILD.z / 2, 0);
  root.add(volume);
  const limits = NEPTUNE_2_MACHINE.nominalAxisLimitsMm;
  const travelSize = {
    x: limits.x.max - limits.x.min,
    y: limits.y.max - limits.y.min,
    z: limits.z.max - limits.z.min,
  };
  const travelVolume = new THREE.LineSegments(
    new THREE.EdgesGeometry(new THREE.BoxGeometry(travelSize.x, travelSize.z, travelSize.y)),
    new THREE.LineBasicMaterial({ color: 0xffbd70, transparent: true, opacity: .22 }),
  );
  travelVolume.position.set(
    (limits.x.min + limits.x.max) / 2 - BUILD.x / 2,
    SURFACE_Y + (limits.z.min + limits.z.max) / 2,
    BUILD.y / 2 - (limits.y.min + limits.y.max) / 2,
  );
  root.add(travelVolume);

  const gantry = new THREE.Group();
  root.add(gantry);
  addBox(gantry, 0, 0, -20, outerWidth - 34, 18, 16, 0x526b83, { metalness: .64, roughness: .33 });
  addBox(gantry, 0, -12, -20, outerWidth - 44, 3, 4, 0xc0d0dc, { metalness: .82, roughness: .25 });
  const carriage = new THREE.Group();
  gantry.add(carriage);
  addBox(carriage, 0, -10, -13, 33, 27, 24, 0x258bd9, { metalness: .18, roughness: .36 });
  addBox(carriage, 0, -23, -4, 12, 8, 13, 0x303f51, { metalness: .5, roughness: .3 });
  const nozzle = new THREE.Mesh(
    new THREE.ConeGeometry(4.2, 10, 10),
    new THREE.MeshStandardMaterial({ color: 0xffbd67, metalness: .38, roughness: .32 }),
  );
  nozzle.rotation.x = Math.PI;
  nozzle.position.set(0, -31, -4);
  carriage.add(nozzle);

  // Moving Y bed carries the plate and the printed object together.
  const bed = new THREE.Group();
  root.add(bed);
  addBox(bed, 0, SURFACE_Y - 5, 0, BUILD.x + 14, 9, BUILD.y + 14, 0x293e55, { metalness: .28, roughness: .42 });
  addBox(bed, 0, SURFACE_Y, 0, BUILD.x, 2, BUILD.y, 0x192d3f, { metalness: .1, roughness: .3 });
  for (let x = -BUILD.x / 2; x <= BUILD.x / 2; x += 20) {
    const line = new THREE.Mesh(new THREE.BoxGeometry(.45, .2, BUILD.y), new THREE.MeshBasicMaterial({ color: 0x385169 }));
    line.position.set(x, SURFACE_Y + 1.2, 0);
    bed.add(line);
  }
  for (let z = -BUILD.y / 2; z <= BUILD.y / 2; z += 20) {
    const line = new THREE.Mesh(new THREE.BoxGeometry(BUILD.x, .2, .45), new THREE.MeshBasicMaterial({ color: 0x385169 }));
    line.position.set(0, SURFACE_Y + 1.2, z);
    bed.add(line);
  }
  for (const x of [-BUILD.x / 2, BUILD.x / 2]) {
    for (const z of [-BUILD.y / 2, BUILD.y / 2]) {
      addBox(bed, x, SURFACE_Y + 3, z, 10, 7, 10, 0x171e29, { metalness: .1, roughness: .8 });
    }
  }

  const switchMarkers = new THREE.Group();
  root.add(switchMarkers);
  makeHomeSwitch(switchMarkers, -outerWidth / 2 + 10, SURFACE_Y + 24, -20, "X minimum reference · schematic");
  makeHomeSwitch(switchMarkers, -BUILD.x / 2, 40, outerDepth / 2 - 10, "Y minimum reference · schematic");
  makeHomeSwitch(switchMarkers, -165, SURFACE_Y + 10, 0, "Z minimum reference · schematic");

  const machine = {
    root, frame, gantry, carriage, nozzle, bed, volume, travelVolume, switchMarkers,
    position: { x: 0, y: 0, z: 0 },
    setMachinePosition(x, y, z) {
      this.position = { x, y, z };
      const mx = x - BUILD.x / 2;
      carriage.position.x = mx;
      gantry.position.y = SURFACE_Y + z + 31;
      // Positive machine Y moves the build plate in the opposite frame direction.
      bed.position.z = y - BUILD.y / 2;
    },
    setFrameTransparent(transparent) {
      this.frame.traverse((child) => {
        if (!child.isMesh || !child.material?.color) return;
        child.material.transparent = transparent;
        child.material.opacity = transparent ? .28 : 1;
        child.material.needsUpdate = true;
      });
    },
  };
  // Centered illustration pose only; Live Sync never treats it as telemetry.
  machine.setMachinePosition(BUILD.x / 2, BUILD.y / 2, BUILD.z / 2);
  return machine;
}

function featureColor(feature, layer, index, total) {
  const normalized = String(feature || "").toLowerCase();
  if (normalized.includes("support")) return new THREE.Color(0x8a78d1);
  if (normalized.includes("infill")) return new THREE.Color(0x51c8ae);
  if (normalized.includes("outer") || normalized.includes("perimeter")) return new THREE.Color(0xffa856);
  if (normalized.includes("inner wall")) return new THREE.Color(0xf0cf68);
  const color = new THREE.Color();
  color.setHSL((.52 + (layer / Math.max(total, 1)) * .25) % 1, .72, .57);
  return color;
}

function ringPoint(center, side, width, height, angle) {
  const cosine = Math.cos(angle), sine = Math.sin(angle);
  return [
    center.x + side.x * width * .5 * cosine,
    center.y + height * .5 * sine,
    center.z + side.z * width * .5 * cosine,
  ];
}

function beadFrame(segment) {
  const from = toBedLocal(segment.from), to = toBedLocal(segment.to);
  let dx = to.x - from.x, dz = to.z - from.z;
  const horizontalLength = Math.hypot(dx, dz);
  if (horizontalLength < 1e-5) { dx = 1; dz = 0; }
  else { dx /= horizontalLength; dz /= horizontalLength; }
  const side = { x: -dz, z: dx };
  const h = Math.max(.08, Math.min(1.2, Number(segment.bead_height_mm) || .2));
  const flowArea = segment.distance_mm > 0
    ? (Number(segment.extrusion_volume_mm3) || 0) / Number(segment.distance_mm)
    : 0;
  const calculatedWidth = flowArea > 0 ? flowArea / h : Number(segment.bead_width_mm) || .42;
  const width = THREE.MathUtils.clamp(calculatedWidth, .28, Math.max(.42, (Number(segment.bead_width_mm) || .42) * 1.6));
  return { from, to, side, width, height: h };
}

function writeRing(positionArray, normalArray, startVertex, center, side, width, height) {
  for (let i = 0; i < RADIAL_STEPS; i += 1) {
    const angle = Math.PI * 2 * i / RADIAL_STEPS;
    const vertex = ringPoint(center, side, width, height, angle);
    const offset = (startVertex + i) * 3;
    positionArray.set(vertex, offset);
    const nx = side.x * Math.cos(angle), ny = Math.sin(angle), nz = side.z * Math.cos(angle);
    const length = Math.hypot(nx, ny, nz) || 1;
    normalArray.set([nx / length, ny / length, nz / length], offset);
  }
}

function makeExtrusionGeometry(segments) {
  const extrusions = segments.filter((segment) => segment.kind === "extrusion" && segment.distance_mm > 0);
  const positions = new Float32Array(extrusions.length * RADIAL_STEPS * 2 * 3);
  const normals = new Float32Array(positions.length);
  const colors = new Float32Array(positions.length);
  const indices = new Uint32Array(extrusions.length * INDEXES_PER_BEAD);
  const color = new THREE.Color();
  let vertexOffset = 0, indexOffset = 0;
  extrusions.forEach((segment, index) => {
    const bead = beadFrame(segment);
    const baseVertex = index * RADIAL_STEPS * 2;
    writeRing(positions, normals, baseVertex, bead.from, bead.side, bead.width, bead.height);
    writeRing(positions, normals, baseVertex + RADIAL_STEPS, bead.to, bead.side, bead.width, bead.height);
    color.copy(featureColor(segment.feature, segment.layer, index, extrusions.length));
    for (let i = 0; i < RADIAL_STEPS * 2; i += 1) colors.set([color.r, color.g, color.b], (baseVertex + i) * 3);
    for (let i = 0; i < RADIAL_STEPS; i += 1) {
      const next = (i + 1) % RADIAL_STEPS;
      const a = baseVertex + i, b = baseVertex + next;
      const c = baseVertex + RADIAL_STEPS + i, d = baseVertex + RADIAL_STEPS + next;
      indices.set([a, c, b, b, c, d], indexOffset);
      indexOffset += 6;
    }
    vertexOffset += RADIAL_STEPS * 2;
  });
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute("normal", new THREE.BufferAttribute(normals, 3));
  geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  geometry.setIndex(new THREE.BufferAttribute(indices, 1));
  geometry.setDrawRange(0, 0);
  geometry.computeBoundingSphere();
  return { geometry, extrusionSegments: extrusions, vertexOffset };
}

function makeFutureLines(segments) {
  const paths = segments.filter((segment) => segment.distance_mm > 0 && ["travel", "extrusion"].includes(segment.kind));
  const positions = new Float32Array(paths.length * 6);
  let offset = 0;
  for (const segment of paths) {
    const from = toBedLocal(segment.from), to = toBedLocal(segment.to);
    positions.set([from.x, from.y + .35, from.z, to.x, to.y + .35, to.z], offset);
    offset += 6;
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geometry.setDrawRange(0, paths.length * 2);
  geometry.computeBoundingSphere();
  return { geometry, paths };
}

function setup(containerId, compact) {
  const container = el(containerId);
  if (!container || container.dataset.twinReady) return;
  container.dataset.twinReady = "true";
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0c1725);
  scene.fog = new THREE.Fog(0x0c1725, 690, 1400);
  const camera = new THREE.PerspectiveCamera(36, 1, .1, 2000);
  // Fit the complete printer silhouette while leaving room to orbit and zoom.
  camera.position.set(720, 650, 760);
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, powerPreference: "low-power" });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, compact ? 1.2 : 1.6));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.2;
  renderer.shadowMap.enabled = !compact;
  renderer.shadowMap.type = THREE.PCFShadowMap;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.domElement.setAttribute("aria-label", "Interactive Neptune 2 bed-slinger kinematic model");
  renderer.domElement.className = "twin-canvas";
  renderer.domElement.style.touchAction = "none";
  container.replaceChildren(renderer.domElement);
  scene.add(new THREE.HemisphereLight(0xd5e9ff, 0x46566c, 3.0));
  const key = new THREE.DirectionalLight(0xffffff, 4.0);
  key.position.set(180, 620, 330);
  key.castShadow = !compact;
  if (!compact) { key.shadow.mapSize.set(768, 768); key.shadow.camera.left = -320; key.shadow.camera.right = 320; key.shadow.camera.top = 650; key.shadow.camera.bottom = -20; }
  scene.add(key);
  const fill = new THREE.DirectionalLight(0x8bbcff, 2.2);
  fill.position.set(-360, 350, -280);
  scene.add(fill);
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(1200, 1200), new THREE.MeshStandardMaterial({ color: 0x111c2a, roughness: .85 }));
  floor.rotation.x = -Math.PI / 2; floor.position.y = 1; floor.receiveShadow = true; scene.add(floor);
  const machine = makeMachine(scene);
  const controls = new OrbitControls(camera, renderer.domElement);
  machine.root.updateMatrixWorld(true);
  const toolheadTarget = machine.nozzle.getWorldPosition(new THREE.Vector3());
  controls.target.copy(toolheadTarget); controls.enableDamping = true; controls.dampingFactor = .08;
  controls.minDistance = 320; controls.maxDistance = 1800; controls.maxPolarAngle = Math.PI * .49;
  controls.enablePan = !compact;
  const instance = { container, compact, scene, camera, renderer, controls, machine, visible: false, raf: 0, lastFrame: 0, deposited: null, future: null, depositedSegments: [], futurePaths: [], toolheadTarget: new THREE.Vector3(), followPausedUntil: 0 };
  controls.addEventListener("start",()=>{instance.followPausedUntil=performance.now()+4500;});
  const resize = new ResizeObserver(() => {
    const width = container.clientWidth, height = container.clientHeight;
    if (!width || !height) return;
    camera.aspect = width / height; camera.updateProjectionMatrix(); renderer.setSize(width, height, false); schedule(instance);
  });
  resize.observe(container);
  const visibility = new IntersectionObserver((entries) => { instance.visible = entries.some((entry) => entry.isIntersecting); if (instance.visible) schedule(instance); else if (instance.raf) { cancelAnimationFrame(instance.raf); instance.raf = 0; } });
  visibility.observe(container);
  controls.addEventListener("change", () => schedule(instance));
  instances.push(instance);
}

function schedule(instance) {
  if (!instance.visible || document.hidden || instance.raf) return;
  instance.raf = requestAnimationFrame((time) => render(instance, time));
}

function render(instance, now) {
  instance.raf = 0;
  const delta = instance.lastFrame ? Math.min(.1, (now - instance.lastFrame) / 1000) : 0;
  instance.lastFrame = now;
  const damping = instance.controls.update();
  if (mode === "simulation" && program && !instance.compact) updateProgramFrame(instance, delta);
  if (mode === "history" && historyPlayback && program && !instance.compact) updateHistoricalFrame(delta);
  instance.machine.root.updateMatrixWorld(true);
  instance.machine.nozzle.getWorldPosition(instance.toolheadTarget);
  const following = now >= instance.followPausedUntil;
  if (following) {
    instance.controls.target.lerp(instance.toolheadTarget, 1 - Math.exp(-Math.max(delta, .016) * 2.8));
  }
  instance.renderer.render(instance.scene, instance.camera);
  if (playing || damping || (following && instance.controls.target.distanceToSquared(instance.toolheadTarget) > 1)) schedule(instance);
}

function clearPath(instance) {
  if (instance.deposited) { instance.machine.bed.remove(instance.deposited); instance.deposited = null; }
  if (instance.future) { instance.machine.bed.remove(instance.future); instance.future = null; }
  instance.depositedSegments = [];
  instance.futurePaths = [];
}

function disposeSharedRender() {
  if (!sharedRender) return;
  sharedRender.geometry.dispose();
  sharedRender.material.dispose();
  sharedRender.futureGeometry.dispose();
  sharedRender.futureMaterial.dispose();
  sharedRender = null;
}

function selectedSegments() {
  if (!program) return [];
  return selectedLayer === "all" ? program.segments : program.segments.filter((segment) => segment.layer === Number(selectedLayer));
}

function showProgram(instance) {
  if (!program) { clearPath(instance); return; }
  if (!sharedRender || sharedRender.program !== program || sharedRender.layer !== selectedLayer) {
    for (const current of instances) clearPath(current);
    disposeSharedRender();
    const segments = selectedSegments();
    const built = makeExtrusionGeometry(segments);
    const future = makeFutureLines(segments);
    sharedRender = {
      program,
      layer: selectedLayer,
      geometry: built.geometry,
      extrusionSegments: built.extrusionSegments,
      material: new THREE.MeshStandardMaterial({ vertexColors: true, roughness: .42, metalness: .02, side: THREE.DoubleSide }),
      futureGeometry: future.geometry,
      futurePaths: future.paths,
      futureMaterial: new THREE.LineBasicMaterial({ color: 0x79c3f4, transparent: true, opacity: .18 }),
    };
  }
  if (instance.deposited) return;
  instance.deposited = new THREE.Mesh(sharedRender.geometry, sharedRender.material);
  instance.deposited.castShadow = true;
  instance.deposited.userData.extrusions = sharedRender.extrusionSegments;
  instance.machine.bed.add(instance.deposited);
  instance.depositedSegments = sharedRender.extrusionSegments;
  instance.future = new THREE.LineSegments(sharedRender.futureGeometry, sharedRender.futureMaterial);
  instance.machine.bed.add(instance.future);
  instance.futurePaths = sharedRender.futurePaths;
  updateProgramFrame(instance, 0);
  schedule(instance);
}

function poseAtTime(seconds) {
  const segments = program.segments;
  let low = 0, high = segments.length;
  while (low < high) {
    const middle = (low + high) >>> 1;
    if (segments[middle].end_seconds <= seconds) low = middle + 1;
    else high = middle;
  }
  const next = segments[low];
  if (next && next.start_seconds <= seconds && seconds < next.end_seconds) {
    const ratio = THREE.MathUtils.clamp((seconds - next.start_seconds) / Math.max(.001, next.end_seconds - next.start_seconds), 0, 1);
    return {
      pose: {
        x: THREE.MathUtils.lerp(next.from.x, next.to.x, ratio),
        y: THREE.MathUtils.lerp(next.from.y, next.to.y, ratio),
        z: THREE.MathUtils.lerp(next.from.z, next.to.z, ratio),
      },
      activeSegment: next,
    };
  }
  const previous = segments[low - 1];
  let pose = previous ? { ...previous.to } : { x: 0, y: 0, z: 0 };
  const previousLine = previous?.line || 0;
  const homes = program.home_events || [];
  let homeLow = 0, homeHigh = homes.length;
  while (homeLow < homeHigh) {
    const middle = (homeLow + homeHigh) >>> 1;
    if (homes[middle].time_seconds <= seconds) homeLow = middle + 1;
    else homeHigh = middle;
  }
  const home = homes[homeLow - 1];
  if (home && home.line > previousLine) pose = { ...home.to };
  return { pose, activeSegment: null };
}

function setDepositedProgress(instance, time, layer = selectedLayer) {
  if (!instance.deposited) return;
  const extrusions = instance.depositedSegments;
  const visible = countFinished(extrusions, time);
  const activeIndex = visible < extrusions.length && extrusions[visible].start_seconds <= time ? visible : -1;
  let drawCount = visible * INDEXES_PER_BEAD;
  if (activeIndex >= 0) {
    const segment = extrusions[activeIndex];
    const progress = THREE.MathUtils.clamp((time - segment.start_seconds) / Math.max(.001, segment.end_seconds - segment.start_seconds), 0, 1);
    const bead = beadFrame(segment);
    const positions = instance.deposited.geometry.attributes.position.array;
    const normals = instance.deposited.geometry.attributes.normal.array;
    const endVertex = activeIndex * RADIAL_STEPS * 2 + RADIAL_STEPS;
    const center = bead.from.clone().lerp(bead.to, progress);
    writeRing(positions, normals, endVertex, center, bead.side, bead.width, bead.height);
    instance.deposited.geometry.attributes.position.needsUpdate = true;
    instance.deposited.geometry.attributes.normal.needsUpdate = true;
    drawCount += INDEXES_PER_BEAD;
  }
  instance.deposited.geometry.setDrawRange(0, drawCount);
  if (instance.future) {
    const completed = countFinished(instance.futurePaths, time);
    instance.future.geometry.setDrawRange(completed * 2, (instance.futurePaths.length - completed) * 2);
  }
}

function countFinished(items, time) {
  let low = 0, high = items.length;
  while (low < high) {
    const middle = (low + high) >>> 1;
    if (items[middle].end_seconds <= time) low = middle + 1;
    else high = middle;
  }
  return low;
}

function updateProgramFrame(instance, delta) {
  if (!program) return;
  const duration = program.estimated_seconds || 0;
  if (playing) {
    replaySeconds += delta * speed;
    if (replaySeconds >= duration) { replaySeconds = duration; playing = false; }
  }
  const { pose, activeSegment } = poseAtTime(replaySeconds);
  instance.machine.setMachinePosition(pose.x, pose.y, pose.z);
  if (!instance.compact) setTwinPosition(pose.x, pose.y, pose.z, activeSegment?.reference_status === "simulated_homed" || (!activeSegment && program.homing_simulated) ? "Planned · simulated homing reference" : "Planned · unreferenced file coordinates");
  setDepositedProgress(instance, replaySeconds);
  updateReplayControls(duration, activeSegment?.layer ?? "—");
  if (playing || instance.controls.update()) schedule(instance);
}

function setTwinPosition(x, y, z, source) {
  const field = el("full-twin-position");
  if (field) field.textContent = `${source} · X ${x.toFixed(1)}, Y ${y.toFixed(1)}, Z ${z.toFixed(1)} mm`;
}

function formatTime(seconds) { const value = Math.max(0, Math.floor(seconds)); return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`; }

function updateReplayControls(duration, layer = "—") {
  const slider = el("twin-time");
  if (!slider) return;
  slider.value = duration ? String(Math.round(replaySeconds / duration * 1000)) : "0";
  const label = el("twin-time-label"); if (label) label.textContent = `${formatTime(replaySeconds)} / ${formatTime(duration)}`;
  const layerLabel = el("twin-layer"); if (layerLabel) layerLabel.textContent = layer === "—" ? "" : `current ${layer}`;
}

function historyDurationMs(session) {
  if (!session?.samples?.length) return 0;
  const start = Date.parse(session.retained_from_at || session.samples[0].observed_at);
  const last = Date.parse(session.samples.at(-1).observed_at);
  return Math.max(0, last - start);
}

function getHistoryModule() {
  if (!historyModulePromise) historyModulePromise = import("/static/twin_history.js");
  return historyModulePromise;
}

function applyHistoryProgress() {
  if (!historyPlayback || !program || !historyProgressAtOffset) return;
  const progress = historyProgressAtOffset(historyPlayback, historyOffsetMs);
  if (!Number.isFinite(progress)) return;
  replaySeconds = program.estimated_seconds * progress / 100;
  for (const instance of instances) updateProgramFrame(instance, 0);
  const slider = el("twin-history-time");
  if (slider) slider.value = String(Math.min(Number(slider.max), historyOffsetMs));
}

function updateHistoricalFrame(delta) {
  if (!historyPlayback || !program) return;
  const duration = historyDurationMs(historyPlayback);
  if (playing) {
    historyOffsetMs = Math.min(duration, historyOffsetMs + delta * 1000 * speed);
    if (historyOffsetMs >= duration) playing = false;
  }
  for (const id of ["twin-play", "twin-history-play"]) if (el(id)) el(id).textContent = playing ? "Pause" : "Play";
  applyHistoryProgress();
}

function normalizedName(value) {
  return String(value || "").replaceAll("\\", "/").split("/").at(-1).trim().toLowerCase();
}

function liveProgress() {
  if (!data || data.stale || !data.connection?.connected) return null;
  const telemetry = data.telemetry || {};
  const state = telemetry.printer_state?.value;
  const active = telemetry.print_active?.value;
  const progressField = telemetry.progress_percent || {};
  const fileField = telemetry.current_file || {};
  const progress = progressField.value;
  const currentFile = fileField.value;
  if (progressField.status !== "available" || fileField.status !== "available") return null;
  if (active !== true || !["printing", "paused"].includes(state) || !Number.isFinite(progress) || progress < 0 || progress > 100) return null;
  if (!program || normalizedName(program.filename) !== normalizedName(currentFile)) return null;
  return { percent: progress, state, currentFile };
}

function updateLive() {
  if (!data || mode !== "live") return;
  const serverPath = data.timeline?.reconstruction;
  const serverFile = data.timeline?.active_session?.filename;
  if (serverPath && program && normalizedName(program.filename) === normalizedName(serverFile)) {
    replaySeconds = Math.max(0, Number(serverPath.elapsed_seconds) || 0);
    for (const instance of instances) {
      if (!instance.compact && !instance.deposited) showProgram(instance);
      updateProgramFrame(instance, 0);
    }
    const source = el("twin-motion-source");
    if (source) source.textContent = `Backend timeline · ${serverPath.progress_percent}% · G-code line ${serverPath.line}`;
    const quality = el("twin-sync-quality");
    if (quality) quality.textContent = "Progress-mapped planned estimate · no firmware-reported position";
    return;
  }
  const progress = liveProgress();
  if (progress && program) {
    replaySeconds = program.estimated_seconds * progress.percent / 100;
    for (const instance of instances) {
      if (!instance.compact && !instance.deposited) showProgram(instance);
      updateProgramFrame(instance, 0);
    }
    const source = el("twin-motion-source");
    if (source) source.textContent = `MKS M27 · ${progress.percent}% · matching ${progress.currentFile}`;
    const quality = el("twin-sync-quality");
    if (quality) quality.textContent = `${progress.state === "paused" ? "Paused" : "Active"} · estimated by file progress, not a reported nozzle coordinate`;
    return;
  }
  const latest = data.motion?.latest;
  const axes = latest?.axes || {};
  if (latest?.fresh && latest.status === "available") {
    const v = Object.fromEntries(["x", "y", "z"].map((axis) => [axis, axes[axis]?.status === "available" ? Number(axes[axis].value_mm) : null]));
    if (Object.values(v).every(Number.isFinite)) {
      for (const instance of instances) instance.machine.setMachinePosition(v.x, v.y, v.z);
      setTwinPosition(v.x, v.y, v.z, "Firmware-reported planned position");
      return;
    }
  }
  const references = data.motion?.operator_reference?.axes || {};
  const confirmed = Object.fromEntries(["x", "y", "z"].map((axis) => {
    const reference = references[axis];
    const position = reference?.position_mm;
    return [axis, reference?.homed && Number.isFinite(position) ? Number(position) : null];
  }));
  const knownAxes = ["x", "y", "z"].filter((axis) => Number.isFinite(confirmed[axis]));
  if (knownAxes.length) {
    for (const instance of instances) {
      const current = instance.machine.position;
      instance.machine.setMachinePosition(
        Number.isFinite(confirmed.x) ? confirmed.x : current.x,
        Number.isFinite(confirmed.y) ? confirmed.y : current.y,
        Number.isFinite(confirmed.z) ? confirmed.z : current.z,
      );
    }
    const values = knownAxes.map((axis) => `${axis.toUpperCase()}≈${confirmed[axis].toFixed(1)} mm`).join(" · ");
    const unknownAxes = ["x", "y", "z"].filter((axis) => !Number.isFinite(confirmed[axis]));
    const observed = knownAxes.every((axis) => {
      const kind = references[axis]?.reference_observation?.kind;
      return kind === "operator_reported_external_homing_reference" || kind === "operator_observed_homing_reference";
    });
    const chip = el("full-twin-position");
    if (chip) chip.textContent = `${observed ? "Operator-reported reference" : "Estimated axis reference"} · ${values} · ${unknownAxes.length ? `${unknownAxes.map((axis) => axis.toUpperCase()).join("/")} illustrative` : "not measured position"}`;
    const compactChip = el("home-twin-position");
    if (compactChip) compactChip.textContent = `Home reference · ${values}`;
    const source = el("twin-motion-source");
    if (source) source.textContent = `${knownAxes.map((axis) => axis.toUpperCase()).join("/")} reference · operator observation`;
    const quality = el("twin-sync-quality");
    if (quality) quality.textContent = "Discrete homing reference only · no movement trace or continuous position telemetry";
    return;
  }
  for (const instance of instances) {
    instance.machine.setMachinePosition(BUILD.x / 2, BUILD.y / 2, BUILD.z / 2);
  }
  const source = el("twin-motion-source");
  if (source) source.textContent = program ? "No matching M27 progress or fresh M114 position" : "Awaiting a selected G-code file; M114 position unavailable";
  const quality = el("twin-sync-quality");
  if (quality) quality.textContent = "Live location unavailable · rendered machine pose is illustrative";
}

function updateReplayFromSlider() {
  if (!program) return;
  const duration = program.estimated_seconds || 0;
  replaySeconds = Number(el("twin-time").value) / 1000 * duration;
  for (const instance of instances) if (!instance.compact) updateProgramFrame(instance, 0);
}

function selectMode(nextMode) {
  mode = nextMode;
  playing = false;
  if (mode === "simulation" && program) for (const instance of instances) showProgram(instance);
  if (mode === "live") updateLive();
  if (mode === "history" && historyPlayback && program && normalizedName(program.filename) === normalizedName(historyPlayback.file_name)) {
    historyOffsetMs = 0;
    applyHistoryProgress();
  }
}

function renderProgramSummary() {
  const status = el("twin-file-status");
  if (!program || !status) return;
  const homeText = program.homing_simulated
    ? ` · G28 simulated for ${program.simulated_homed_axes.join("/").toUpperCase()}`
    : " · no G28 reference in file";
  const warningText = program.warnings.length ? ` · ${program.warnings.join(" ")}` : "";
  status.textContent = `${program.filename} · ${program.segment_count.toLocaleString()} moves · ${program.layers.length} layers · estimated ${formatTime(program.estimated_seconds)}${homeText} · local only${warningText}`;
  const homeStatus = el("twin-homing-status");
  if (homeStatus) homeStatus.textContent = program.homing_simulated
    ? `This file simulates G28 for ${program.simulated_homed_axes.join(", ").toUpperCase()}. It does not confirm physical homing.`
    : "This file has no parsed G28; the planned path has no simulated endstop reference.";
  const machineStatus = el("twin-machine-limits");
  if (machineStatus) machineStatus.textContent = "Printable volume 220 × 220 × 250 mm · factory V1.0.4 travel defaults X/Y 235 mm, Z 260 mm · active firmware and EEPROM unverified";
  const features = el("twin-feature-summary");
  if (features) features.textContent = program.features.length ? `Features: ${program.features.join(" · ")}` : "Feature types unavailable in this G-code header.";
}

function bindControls() {
  const fileInput = el("twin-gcode-file");
  fileInput?.addEventListener("change", async () => {
    const file = fileInput.files?.[0]; if (!file) return;
    const status = el("twin-file-status");
    status.textContent = `Reading ${file.name} locally…`;
    if (file.size > 50 * 1024 * 1024) { status.textContent = "File exceeds 50 MB preview limit."; return; }
    try {
      await loadGcodeText(await file.text(), file.name, true);
    } catch (error) { status.textContent = `Could not parse local file: ${error.message}`; }
  });
  el("twin-demo-load")?.addEventListener("click", async () => {
    const status = el("twin-file-status");
    status.textContent = "Loading simulated movement path…";
    try {
      const response = await fetch("/api/timeline/offline-demo", {credentials:"same-origin"});
      const demo = await response.json();
      if (!response.ok) throw new Error(demo.detail || `Demo unavailable (${response.status})`);
      loadAnalyzedDemo(demo);
      status.textContent = "Built-in offline movement demo · interpreted by the shared server timeline · never sent to the printer.";
    } catch (error) { status.textContent = `Could not load offline demo: ${error.message}`; }
  });
  el("twin-time")?.addEventListener("input", () => { playing = false; updateReplayFromSlider(); });
  el("twin-speed")?.addEventListener("change", (event) => { speed = Number(event.target.value) || 1; });
  el("twin-layer-select")?.addEventListener("change", (event) => {
    selectedLayer = event.target.value;
    replaySeconds = selectedLayer === "all" ? 0 : (program?.segments.find((segment) => segment.layer === Number(selectedLayer))?.start_seconds || 0);
    for (const instance of instances) showProgram(instance);
  });
  el("twin-play")?.addEventListener("click", () => {
    if (!program) return;
    if (mode === "history" && historyPlayback) {
      if (historyOffsetMs >= historyDurationMs(historyPlayback)) historyOffsetMs = 0;
    } else if (replaySeconds >= (program.estimated_seconds || 0)) replaySeconds = 0;
    playing = !playing; el("twin-play").textContent = playing ? "Pause" : "Play";
    for (const instance of instances) schedule(instance);
  });
  el("twin-reset")?.addEventListener("click", () => { playing = false; replaySeconds = 0; historyOffsetMs = 0; el("twin-play").textContent = "Play"; if (mode === "history") applyHistoryProgress(); else updateReplayFromSlider(); });
  el("twin-frame-transparent")?.addEventListener("change", (event) => { for (const instance of instances) instance.machine.setFrameTransparent(event.target.checked); });
  el("twin-history-refresh")?.addEventListener("click", () => refreshHistoryList());
  el("twin-history-load")?.addEventListener("click", () => loadHistorySession());
  el("twin-history-play")?.addEventListener("click", () => {
    if (!historyPlayback || !program) return;
    if (historyOffsetMs >= historyDurationMs(historyPlayback)) historyOffsetMs = 0;
    playing = !playing;
    for (const id of ["twin-play", "twin-history-play"]) if (el(id)) el(id).textContent = playing ? "Pause" : "Play";
    for (const instance of instances) schedule(instance);
  });
  el("twin-history-reset")?.addEventListener("click", () => {
    playing = false;
    historyOffsetMs = 0;
    for (const id of ["twin-play", "twin-history-play"]) if (el(id)) el(id).textContent = "Play";
    applyHistoryProgress();
  });
  el("twin-history-speed")?.addEventListener("change", (event) => { speed = Number(event.target.value) || 1; });
  el("twin-history-select")?.addEventListener("change", () => { historyPlayback = null; updateHistoryAvailability(); });
  el("twin-history-time")?.addEventListener("input", (event) => {
    playing = false;
    historyOffsetMs = Number(event.target.value) || 0;
    for (const id of ["twin-play", "twin-history-play"]) if (el(id)) el(id).textContent = "Play";
    applyHistoryProgress();
  });
  window.addEventListener("neptune:twin-mode", (event) => selectMode(event.detail));
  document.addEventListener("visibilitychange", () => { for (const instance of instances) { if (document.hidden && instance.raf) { cancelAnimationFrame(instance.raf); instance.raf = 0; } else schedule(instance); } });
}

function loadAnalyzedDemo(analysis) {
  program = {
    filename: analysis.filename,
    segments: analysis.segments.map((segment) => ({
      ...segment,
      layer: 0,
      feature: "offline demonstration",
      reference_status: "simulated_homed",
      bead_width_mm: .42,
      bead_height_mm: .2,
      extrusion_volume_mm3: Math.max(0, Number(segment.extrusion_delta_mm) || 0) * Math.PI * (.875 ** 2),
    })),
    segment_count: analysis.segments.length,
    estimated_seconds: analysis.duration_seconds,
    layers: [0],
    features: [],
    warnings: [],
    homing_simulated: true,
    simulated_homed_axes: ["x", "y", "z"],
    home_events: [],
  };
  replaySeconds = 0; playing = false; selectedLayer = "all";
  const layerSelect = el("twin-layer-select");
  layerSelect.replaceChildren(new Option("All layers", "all"), new Option("Layer 1", "0"));
  layerSelect.disabled = !program.segment_count;
  el("twin-time").disabled = !program.segment_count;
  el("twin-play").disabled = !program.segment_count;
  el("twin-reset").disabled = !program.segment_count;
  renderProgramSummary();
  for (const instance of instances) showProgram(instance);
  updateReplayControls(program.estimated_seconds);
  if (mode === "live") updateLive();
  updateHistoryAvailability();
}

async function loadGcodeText(gcodeText, filename, persist) {
  const status = el("twin-file-status");
  program = parseToolpath(gcodeText, filename);
  replaySeconds = 0; playing = false; selectedLayer = "all";
  const layerSelect = el("twin-layer-select");
  layerSelect.replaceChildren(new Option("All layers", "all"));
  for (const layer of program.layers) layerSelect.add(new Option(`Layer ${layer + 1}`, String(layer)));
  layerSelect.disabled = !program.segment_count;
  el("twin-time").disabled = !program.segment_count;
  el("twin-play").disabled = !program.segment_count;
  el("twin-reset").disabled = !program.segment_count;
  renderProgramSummary();
  if (persist) {
    status.textContent = `${filename} · indexing planned motion on Observatory…`;
    const response = await fetch("/api/timeline/gcode", {method:"POST", credentials:"same-origin", headers:{"Content-Type":"application/json"}, body:JSON.stringify({filename,content:gcodeText})});
    const indexed = await response.json();
    if (!response.ok) throw new Error(indexed.detail || `Server analysis failed (${response.status})`);
    status.textContent = `${filename} · ${indexed.segment_count.toLocaleString()} planned moves indexed · ${formatTime(indexed.estimated_duration_seconds)} simplified timing estimate · analysis only, not sent to printer.`;
  } else {
    status.textContent = `${filename} · ${program.segment_count.toLocaleString()} planned moves · offline simulation only.`;
  }
  for (const instance of instances) showProgram(instance);
  updateReplayControls(program.estimated_seconds);
  if (mode === "live") updateLive();
  if (historyPlayback && normalizedName(program.filename) === normalizedName(historyPlayback.file_name)) await activateHistorySession(historyPlayback);
  updateHistoryAvailability();
}

async function refreshHistoryList() {
  const select = el("twin-history-select");
  const note = el("twin-history-note");
  if (!select) return;
  try {
    const { progressAtOffset } = await getHistoryModule();
    historyProgressAtOffset = progressAtOffset;
    const timeline = await fetch("/api/timeline", {credentials:"same-origin"}).then(async (response) => { const body=await response.json(); if(!response.ok)throw new Error(body.detail||"Could not load server print history"); return body; });
    const sessions = timeline.sessions || [];
    select.replaceChildren(new Option("Choose a recorded print", ""));
    for (const session of sessions) {
      const label = `${session.filename || "Filename unavailable"} · ${new Date(session.started_at).toLocaleString()} · ${session.state}`;
      select.add(new Option(label, session.id));
    }
    if (note) note.textContent = sessions.length
      ? "Print history is recorded by the server-side telemetry worker, including while this browser is closed. Select the matching G-code in Simulation to replay its planned path against recorded MKS progress."
      : "No server-recorded print sessions yet. The backend records automatically when the shared poller sees an active print.";
    updateHistoryAvailability();
  } catch (error) {
    if (note) note.textContent = `Could not load server print history: ${error.message}`;
  }
}

async function loadHistorySession() {
  const select = el("twin-history-select");
  const note = el("twin-history-note");
  const id = select?.value;
  if (!id) { if (note) note.textContent = "Choose a recorded print first."; return; }
  try {
    const { progressAtOffset } = await getHistoryModule();
    historyProgressAtOffset = progressAtOffset;
    const session = await fetch(`/api/timeline/sessions/${encodeURIComponent(id)}`, {credentials:"same-origin"}).then(async (response) => { const body=await response.json(); if(!response.ok)throw new Error(body.detail||"The recorded session is unavailable"); return body; });
    session.file_name = session.filename;
    session.samples = (session.telemetry || []).filter((sample)=>Number.isFinite(sample.progress)).map((sample)=>({observed_at:sample.observed_at,progress_percent:sample.progress}));
    session.retained_from_at = session.samples[0]?.observed_at || session.started_at;
    if (!session.samples.length) throw new Error("This session has no usable MKS progress samples.");
    historyPlayback = session;
    if (!program || normalizedName(program.filename) !== normalizedName(session.file_name)) {
      if (note) note.textContent = `Select the matching G-code file “${session.file_name}” in Offline G-code Simulation first. It is used only to reconstruct a planned path and is never sent to the printer.`;
      return;
    }
    await activateHistorySession(session);
    updateHistoryAvailability();
  } catch (error) { if (note) note.textContent = error.message; }
}

async function activateHistorySession(session) {
  const note = el("twin-history-note");
  const duration = historyDurationMs(session);
  if (!session.samples?.length || !program || normalizedName(program.filename) !== normalizedName(session.file_name)) {
    if (note) note.textContent = `Choose the local G-code file named “${session.file_name}” before loading this session.`;
    return;
  }
  historyPlayback = session;
  historyOffsetMs = 0;
  replaySeconds = 0;
  playing = false;
  mode = "history";
  const slider = el("twin-history-time");
  if (slider) { slider.min = "0"; slider.max = String(Math.max(1, duration)); slider.value = "0"; slider.disabled = duration <= 0; }
  if (note) note.textContent = `Loaded ${session.samples.length} server-recorded MKS samples over ${formatTime(duration / 1000)}. Toolhead location is an estimate mapped from reported progress onto the matching G-code timeline; no exact line or firmware position was recorded.`;
  for (const instance of instances) showProgram(instance);
  applyHistoryProgress();
  if (el("twin-play")) el("twin-play").textContent = "Play";
}

function updateHistoryAvailability() {
  const button = el("twin-history-load");
  if (button) button.disabled = !el("twin-history-select")?.value;
  const canPlay = Boolean(historyPlayback?.samples?.length && program && normalizedName(program.filename) === normalizedName(historyPlayback.file_name));
  for (const id of ["twin-history-play", "twin-history-reset"]) if (el(id)) el(id).disabled = !canPlay;
}

export function initializeTwins() {
  for (const [id, compact] of [["home-twin", true], ["full-twin", false]]) {
    try { setup(id, compact); }
    catch (error) { const target = el(id); if (target) target.innerHTML = `<div class="chart-empty visible">3D preview unavailable in this browser: ${String(error.message || error)}</div>`; }
  }
  bindControls();
  updateTelemetry(data);
  refreshHistoryList();
}

export { refreshHistoryList };

export function updateTelemetry(snapshot) {
  if (snapshot) data = snapshot;
  if (!data) return;
  const telemetry = data.telemetry || {};
  const nozzle = telemetry.nozzle_temperature || {}, bed = telemetry.bed_temperature || {};
  const status = telemetry.printer_state?.value || (data.stale ? "stale" : "unknown");
  for (const id of ["home-twin-state", "full-twin-state"]) if (el(id)) el(id).textContent = `Printer · ${status}`;
  for (const [id, value] of [["home-twin-nozzle", nozzle], ["full-twin-nozzle", nozzle], ["home-twin-bed", bed], ["full-twin-bed", bed]]) {
    if (el(id)) el(id).textContent = `${id.includes("nozzle") ? "Nozzle" : "Bed"} · ${Number.isFinite(value.value) ? `${value.value.toFixed(1)}°C` : "unavailable"}`;
  }
  const telemetryProgress = telemetry.progress_percent || {};
  const progressLabel = telemetryProgress.status === "available" ? `${telemetryProgress.value}% · M27` : "Progress unavailable";
  for (const id of ["full-twin-progress", "home-twin-progress"]) if (el(id)) el(id).textContent = progressLabel;
  const printActive = telemetry.print_active?.value === true;
  const sessionStatus = el("twin-recording-status");
  if (sessionStatus) sessionStatus.textContent = data.timeline?.active_session
    ? `Server recording · ${data.timeline.active_session.filename || "active print"}`
    : printActive ? "Server recording · MKS file/progress association pending" : "Server recorder ready · shared poller";
  updateLive();
  for (const instance of instances) schedule(instance);
  if (mode === "history" && historyPlayback && program) updateProgramFrame(instances.find((instance) => !instance.compact), 0);
}
