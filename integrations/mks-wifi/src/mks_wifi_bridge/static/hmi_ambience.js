import {readPreferences,subscribePreferences} from '/static/hmi_preferences.js';
/* Adapter: existing HMI lifecycle + authenticated shared telemetry, one renderer. */
import {createSculpture} from '/static/sculpture/scene.js';
import {createVisualStore,activeGeometry} from '/static/sculpture/store.js';
const host=document.getElementById('hmi-ambience');
try{
 const store=createVisualStore({externalHistory:true});let engine,artifactKey=null,request=null,cinematic=false,inFlight=false,artifactAttempt=0;
 let preferences=readPreferences(),previewOverride=null;
 engine=createSculpture(host,{profile:preferences.profile,onMode:(id,mode)=>{
  const caption=document.getElementById('cinema-shot');if(caption)caption.textContent=mode.title;
  const kicker=document.querySelector('.cinema-kicker');if(kicker)kicker.textContent=mode.subtitle;
  const label=document.getElementById('studio-mode');if(label)label.textContent=mode.title;
 },onMetrics:m=>{
  host.dataset.fps=m.fps.toFixed(1);host.dataset.profile=m.profile;
  window.dispatchEvent(new CustomEvent('hmi:renderer-metrics',{detail:m}));
 }});
 const apply=next=>{preferences=next;engine.setProfile(next.profile);engine.setMode(previewOverride??next.mode);engine.setTelemetryEffects(next.telemetryEffects);engine.setAdaptive(next.adaptiveQuality);};
 const stopPreferences=subscribePreferences(apply);
 const unsubscribe=store.subscribe(state=>{
  engine.setState(state);
  const key=(state.job.active||state.phase==='completed')?state.job.filename:null;
  if(key===artifactKey&&(engine.geometryCount||inFlight||Date.now()-artifactAttempt<30000))return;
  artifactKey=key;artifactAttempt=Date.now();request?.abort();
  if(!key){request=null;inFlight=false;engine.setGeometry(null);return;}
  engine.setGeometry(null);request=new AbortController();inFlight=true;const expected=key;
  activeGeometry(state,request.signal).then(model=>{if(artifactKey===expected)engine.setGeometry(model);}).catch(()=>{}).finally(()=>{if(artifactKey===expected){inFlight=false;request=null;}});
 });
 let view='overview',inspectorOpen=false;
 const visibility=()=>{const active=cinematic||(view==='overview'&&!inspectorOpen);engine.setVisible(active);host.style.display=active?'':'none';};
 const viewChanged=e=>{view=e.detail.view;visibility();};window.addEventListener('hmi:view',viewChanged);
 const transition=e=>{cinematic=e.detail.active;visibility();engine.setCinematic(cinematic);if(!cinematic){previewOverride=null;engine.setMode(preferences.mode);}};window.addEventListener('hmi:cinematic',transition);
 const preview=e=>{previewOverride=e.detail?.mode||null;engine.setMode(previewOverride||preferences.mode);};
 window.addEventListener('hmi:studio-preview',preview);
 const inspector=e=>{inspectorOpen=e.detail.open;visibility();host.dataset.suspended=String(e.detail.open);};
 window.addEventListener('hmi:inspector',inspector);
 host.dataset.renderer='ready';
 addEventListener('pagehide',event=>{if(event.persisted)return;request?.abort();unsubscribe();store.dispose();window.removeEventListener('hmi:view',viewChanged);window.removeEventListener('hmi:inspector',inspector);window.removeEventListener('hmi:cinematic',transition);window.removeEventListener('hmi:studio-preview',preview);stopPreferences();engine.dispose();});
}catch(error){host.dataset.renderer='unavailable';console.warn('Sculpture renderer unavailable',error.message);}
