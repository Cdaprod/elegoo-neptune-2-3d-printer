/* Pure normalization. Values retain provenance; unavailable is never zero. */
export const MODES=Object.freeze(['ambient','architecture','thermodynamics','fabrication','intelligence']);
const finite=v=>typeof v==='number'&&Number.isFinite(v);
export const clamp=(v,a=0,b=1)=>Math.min(b,Math.max(a,v));
function field(raw,kind='measured',source='telemetry',simulation=false){
 if(!raw||raw.status!=='available'||!finite(raw.value))return {value:null,kind:'unavailable',source:raw?.source_command||source};
 return {value:raw.value,kind:simulation?'simulated':(['measured','commanded','inferred','simulated'].includes(raw.provenance)?raw.provenance:kind),source:raw.source_command||source};
}
export function normalize(snapshot={},history=[],now=Date.now()){
 const t=snapshot.telemetry||{},sim=snapshot.simulation===true,connected=snapshot.connection?.connected===true;
 const observed=Date.parse(snapshot.observed_at),age=finite(snapshot.data_age_seconds)?Math.max(0,snapshot.data_age_seconds):Number.isFinite(observed)?Math.max(0,(now-observed)/1000):null;
 const signal=connected&&age!==null?Math.exp(-Math.max(0,age-4)/14)*(snapshot.stale&&age<5?.35:1):0;
 const read=(key,kind='measured')=>field(t[key],kind,key,sim);
 const nozzle=read('nozzle_temperature'),bed=read('bed_temperature');
 const target=(key,legacy)=>field(t[legacy]||(t[key]?.status==='available'&&finite(t[key].target_c)?{status:'available',value:t[key].target_c}:null),'commanded',key+' target setpoint',sim);
 const nozzleTarget=target('nozzle_temperature','nozzle_target'),bedTarget=target('bed_temperature','bed_target');
 const motion=snapshot.motion?.latest,axes={};
 for(const axis of ['x','y','z']){const raw=motion?.axes?.[axis];axes[axis]=field(raw?{...raw,value:raw.value_mm}:null,raw?.provenance==='measured'?'measured':'commanded',raw?.source_command||'M114 firmware planned coordinate',sim);if(motion?.fresh!==true)axes[axis]={...axes[axis],fresh:false};else axes[axis].fresh=true;}
 const fan=read('fan_speed'),feed=read('feedrate');
 if(fan.value!==null&&(fan.value<0||fan.value>100))fan.value=null,fan.kind='unavailable';
 const job=snapshot.print_job||{},progress=field(job.progress_percent,'measured','reported file progress',sim);
 if(progress.value!==null&&(progress.value<0||progress.value>100))progress.value=null,progress.kind='unavailable';
 const layer=read('current_layer'),layerHeight=read('layer_height');
 const rows=history.filter(p=>finite(p.nozzle_c)||finite(p.bed_c)).slice(-160).map(p=>({time:p.observed_at||p.timestamp||null,nozzle:finite(p.nozzle_c)?p.nozzle_c:null,bed:finite(p.bed_c)?p.bed_c:null,nozzleTarget:finite(p.nozzle_target_c)?p.nozzle_target_c:null,bedTarget:finite(p.bed_target_c)?p.bed_target_c:null}));
 const reported=String(t.printer_state?.value||job.status||'unknown').toLowerCase();
 let phase='unavailable',phaseKind=sim?'simulated':'reported';
 if(!connected)phase='disconnected';
 else if(/error|fault/.test(reported))phase='fault';
 else if(snapshot.stale)phase='unavailable';
 else if(/paused/.test(reported))phase='paused';
 else if(/printing|running/.test(reported)||job.active===true)phase='printing';
 else if(/complete|finished/.test(reported))phase='completed';
 else if((nozzle.value!==null&&nozzleTarget.value!==null&&nozzleTarget.value-nozzle.value>5)||(bed.value!==null&&bedTarget.value!==null&&bedTarget.value-bed.value>5))phase='heating',phaseKind=sim?'simulated':'inferred';
 else if(rows.length>2&&rows[0].nozzle!==null&&rows.at(-1).nozzle!==null&&rows[0].nozzle-rows.at(-1).nozzle>3&&nozzleTarget.value!==null&&nozzle.value>nozzleTarget.value+5)phase='cooling',phaseKind=sim?'simulated':'inferred';
 else if(/idle|operational|ready/.test(reported))phase='idle';
 const latency=finite(snapshot.poll_latency_ms)?{value:snapshot.poll_latency_ms,kind:sim?'simulated':'measured',source:'backend poll round trip'}:{value:null,kind:'unavailable',source:'backend poll round trip'};
 return {connected,simulation:sim,observedAt:Number.isFinite(observed)?observed:null,receivedAt:now,age,signal,phase,phaseKind,nozzle,nozzleTarget,bed,bedTarget,fan,feed,axes,progress,layer,layerHeight,latency,history:rows,job:{filename:job.filename?.status==='available'?job.filename.value:null,active:job.active===true,status:job.status||'unknown'},positionKind:Object.values(axes).some(a=>a.value!==null)?[...new Set(Object.values(axes).filter(a=>a.value!==null).map(a=>a.kind))].join('/'):'schematic'};
}
export function freshness(state,now=Date.now()){if(!state.connected||state.age===null)return 0;if(state.simulation)return state.signal;return state.signal*Math.exp(-Math.max(0,(now-state.receivedAt)/1000-3)/14);}
export function chooseMode(state,time,hasGeometry=false){
 if(!state.connected||state.signal<.12||state.phase==='unavailable')return 'ambient';
 const thermal=state.nozzle.value!==null||state.bed.value!==null;
 const t=time%90;if(t<15||t>=85)return 'ambient';if(t<30)return 'architecture';if(t<50)return thermal?'thermodynamics':'architecture';if(t<70)return thermal&&(state.phase==='heating'||state.phase==='cooling')?'thermodynamics':hasGeometry?'fabrication':'architecture';return state.history.length||state.latency.value!==null?'intelligence':'ambient';
}
export class AdaptiveProfile{
 constructor(name='balanced'){this.set(name);this.samples=0;this.average=0;}
 set(name){this.name=['low','balanced','cinematic'].includes(name)?name:'balanced';this.target=this.name==='low'?20:this.name==='cinematic'?30:24;this.scale=this.name==='low'?.75:this.name==='cinematic'?1.2:1;this.maximum=this.scale;this.particles=this.name==='low'?64:this.name==='cinematic'?256:128;this.bloom=this.name!=='low';this.samples=0;this.average=0;}
 sample(ms){this.average=this.average?this.average*.94+ms*.06:ms;if(++this.samples%90)return false;const before=this.scale;if(this.average>1000/this.target*.85)this.scale=Math.max(.45,this.scale-.1);else if(this.average<1000/this.target*.4)this.scale=Math.min(this.maximum,this.scale+.025);return before!==this.scale;}
}
