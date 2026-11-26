import * as T from '/static/vendor/three.module.js';
import {makeMachine} from '/static/digital_twin.js';
function profileGeometry(dimensions){
 const axis=dimensions.indexOf(Math.max(...dimensions)),u=(axis+1)%3,v=(axis+2)%3,w=dimensions[u]/2,h=dimensions[v]/2,g=Math.min(w,h)*.23;
 const shape=new T.Shape();const pts=[[-w,-h],[-g,-h],[-g,-h+g],[g,-h+g],[g,-h],[w,-h],[w,-g],[w-g,-g],[w-g,g],[w,g],[w,h],[g,h],[g,h-g],[-g,h-g],[-g,h],[-w,h],[-w,g],[-w+g,g],[-w+g,-g],[-w,-g]];
 pts.forEach(([x,y],i)=>i?shape.lineTo(x,y):shape.moveTo(x,y));shape.closePath();const hole=new T.Path();hole.absarc(0,0,Math.min(w,h)*.24,0,Math.PI*2,true);shape.holes.push(hole);
 const geometry=new T.ExtrudeGeometry(shape,{depth:dimensions[axis]-.6,steps:1,bevelEnabled:true,bevelSegments:1,bevelThickness:.3,bevelSize:.3,curveSegments:6});geometry.translate(0,0,-dimensions[axis]/2+.3);
 const basis=new T.Matrix4(),a=new T.Vector3(),b=new T.Vector3(),c=new T.Vector3();a.setComponent(u,1);b.setComponent(v,1);c.setComponent(axis,1);basis.makeBasis(a,b,c);geometry.applyMatrix4(basis);return geometry;
}
function mesh(parent,geometry,material,x,y,z){const m=new T.Mesh(geometry,material);m.position.set(x,y,z);parent.add(m);return m;}
function mergeStatic(group,exclude=new Set()){
 group.updateMatrixWorld(true);const inverse=new T.Matrix4().copy(group.matrixWorld).invert(),matrix=new T.Matrix4(),normal=new T.Matrix3(),p=new T.Vector3(),n=new T.Vector3(),positions=[],normals=[],colors=[],surfaces=[];const removals=[];
 group.traverse(o=>{if(!o.isMesh||exclude.has(o)||o.isInstancedMesh)return;const g=o.geometry.index?o.geometry.toNonIndexed():o.geometry.clone();matrix.multiplyMatrices(inverse,o.matrixWorld);normal.getNormalMatrix(matrix);const color=o.material.color||new T.Color(0x648095);for(let i=0;i<g.attributes.position.count;i++){p.fromBufferAttribute(g.attributes.position,i).applyMatrix4(matrix);n.fromBufferAttribute(g.attributes.normal,i).applyMatrix3(normal).normalize();positions.push(p.x,p.y,p.z);normals.push(n.x,n.y,n.z);colors.push(color.r,color.g,color.b);surfaces.push(o.material.metalness||0,o.material.roughness??.7);}g.dispose();removals.push(o);});
 for(const o of removals){o.removeFromParent();o.geometry.dispose();o.material.dispose();}if(!positions.length)return;
 const geometry=new T.BufferGeometry();geometry.setAttribute('position',new T.Float32BufferAttribute(positions,3));geometry.setAttribute('normal',new T.Float32BufferAttribute(normals,3));geometry.setAttribute('color',new T.Float32BufferAttribute(colors,3));geometry.setAttribute('surface',new T.Float32BufferAttribute(surfaces,2));const material=new T.MeshStandardMaterial({vertexColors:true,metalness:.72,roughness:.34});material.userData.surfaceAttribute=true;const combined=new T.Mesh(geometry,material);group.add(combined);return combined;
}
export function createMachine(scene){
 const m=makeMachine(scene);m.setMachinePosition(110,110,85);m.switchMarkers.visible=false;m.volume.visible=false;m.travelVolume.visible=false;
 m.frame.traverse(o=>{const p=o.geometry?.parameters;if(o.isMesh&&p?.width&&Math.max(p.width,p.height,p.depth)>150&&Math.min(p.width,p.height,p.depth)>=14){const old=o.geometry;o.geometry=profileGeometry([p.width,p.height,p.depth]);old.dispose();}});
 const metal=new T.MeshStandardMaterial({color:0xa1b3bd,metalness:.9,roughness:.26}),rubber=new T.MeshStandardMaterial({color:0x111c25,roughness:.9}),polymer=new T.MeshStandardMaterial({color:0x234452,roughness:.58,metalness:.12});
 for(const x of [-165,165]){
  const curve=new class extends T.Curve{getPoint(t,target=new T.Vector3()){return target.set(x+2.9*Math.cos(t*90*Math.PI),115+t*360,2.9*Math.sin(t*90*Math.PI));}}();
  mesh(m.frame,new T.TubeGeometry(curve,360,.65,4,false),metal.clone(),0,0,0);
  mesh(m.frame,new T.BoxGeometry(40,38,40),rubber.clone(),x,80,0);mesh(m.frame,new T.BoxGeometry(42,4,42),metal.clone(),x,101,0);
  for(const z of [-15,15])mesh(m.frame,new T.CylinderGeometry(2,2,4,8),metal.clone(),x+14,105,z);
 }
 // Carriage rollers, distinct polymer housing and recognizable cooling assembly.
 for(const x of [-19,19])for(const y of [-2,-20]){const wheel=mesh(m.carriage,new T.CylinderGeometry(7,7,5,16),rubber.clone(),x,y,-25);wheel.rotation.x=Math.PI/2;const hub=mesh(m.carriage,new T.CylinderGeometry(2.5,2.5,6,12),metal.clone(),x,y,-25);hub.rotation.x=Math.PI/2;}
 for(let i=0;i<6;i++)mesh(m.carriage,new T.CylinderGeometry(6.2,6.2,1.1,16),metal.clone(),0,-11-i*2,-4);
 const fanRing=mesh(m.carriage,new T.TorusGeometry(10.5,2,6,24),polymer.clone(),0,-7,4);
 const blades=new T.InstancedMesh(new T.BoxGeometry(3,8,.6),new T.MeshStandardMaterial({color:0x344e59,metalness:.25,roughness:.6}),7),matrix=new T.Matrix4();for(let i=0;i<7;i++){matrix.makeRotationZ(i*Math.PI*2/7);matrix.setPosition(Math.cos(i*Math.PI*2/7)*5,Math.sin(i*Math.PI*2/7)*5,0);blades.setMatrixAt(i,matrix);}blades.position.copy(fanRing.position);m.carriage.add(blades);m.fan=blades;
 const cableCurve=new T.CatmullRomCurve3([new T.Vector3(-190,50,-20),new T.Vector3(-210,220,-25),new T.Vector3(-190,440,-20),new T.Vector3(-80,475,-20)]);
 mesh(m.frame,new T.TubeGeometry(cableCurve,42,3,6,false),rubber.clone(),0,0,0);
 // Consolidate immutable detail into buffers; moving groups retain independent transforms.
 const oldGrid=m.bed.children.filter(o=>o.geometry?.parameters?.height===.2);for(const o of oldGrid){o.removeFromParent();o.geometry.dispose();o.material.dispose();}
 mergeStatic(m.frame);mergeStatic(m.carriage,new Set([m.nozzle,blades]));mergeStatic(m.bed);m.bodyMaterials=[];m.root.traverse(o=>{if(o.isMesh&&o.material.isMeshStandardMaterial)m.bodyMaterials.push(o.material);});metal.dispose();rubber.dispose();polymer.dispose();
 return m;
}
