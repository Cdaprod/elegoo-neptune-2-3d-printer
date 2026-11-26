import * as T from '/static/vendor/three.module.js';
export function createFabrication(parent){
 const group=new T.Group();parent.add(group);let completed=null,future=null,active=null,count=0,model=null;const clip=new T.Plane(new T.Vector3(0,-1,0),0);let clipping=false;
 const dispose=()=>{group.traverse(o=>{o.geometry?.dispose();o.material?.dispose();});group.clear();count=0;model=null;};
 function load(value){dispose();if(!value?.segments?.length)return;model=value;const values=new Float32Array(value.segments.length*6);for(let i=0;i<value.segments.length;i++){const s=value.segments[i];values.set([s[0]-110,40+s[2],110-s[1],s[3]-110,40+s[5],110-s[4]],i*6);}count=value.segments.length;const geometry=new T.BufferGeometry();geometry.setAttribute('position',new T.BufferAttribute(values,3));completed=new T.LineSegments(geometry,new T.LineBasicMaterial({color:0xffd69a,transparent:true,opacity:1}));future=new T.LineSegments(geometry.clone(),new T.LineBasicMaterial({color:0x4b98a0,transparent:true,opacity:.14,depthWrite:false}));active=new T.LineSegments(geometry.clone(),new T.LineBasicMaterial({color:0xffffff,transparent:true,opacity:1}));group.add(future,completed,active);}
 function update(state,weight,signal){group.visible=weight>.005&&count>0;if(!count)return;const progress=(state.job.active||state.phase==='completed')&&state.progress.value!==null?Math.floor(count*state.progress.value/100):null;
  completed.geometry.setDrawRange(0,progress===null?0:progress*2);future.geometry.setDrawRange(progress===null?0:progress*2,(count-(progress||0))*2);active.geometry.setDrawRange(progress===null?0:Math.max(0,progress-1)*2,progress===null?0:Math.min(16,count-progress)*2);
  const shouldClip=state.layer.value!==null&&state.layerHeight.value!==null;if(clipping!==shouldClip){clipping=shouldClip;completed.material.clippingPlanes=clipping?[clip]:null;completed.material.needsUpdate=true;}clip.constant=38+(state.layer.value||0)*(state.layerHeight.value||0);
  completed.material.opacity=weight*(.25+.75*signal);future.material.opacity=weight*.2;active.material.opacity=weight*signal;group.position.z=state.axes.y.value!==null?(state.axes.y.value-110):0;
 }
 return {group,load,update,dispose,get model(){return model;},get count(){return count;}};
}
