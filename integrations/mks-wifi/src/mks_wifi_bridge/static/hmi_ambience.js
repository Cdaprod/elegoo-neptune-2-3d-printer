/* Adapter: existing HMI lifecycle + authenticated shared telemetry, one renderer. */
import {createSculpture} from '/static/sculpture/scene.js';
import {createVisualStore,activeGeometry} from '/static/sculpture/store.js';
const host=document.getElementById('hmi-ambience');
try{
 const store=createVisualStore({externalHistory:true});let engine,artifactKey=null,request=null,cinematic=false,inFlight=false,artifactAttempt=0;
 let savedProfile='balanced';try{savedProfile=localStorage.getItem('neptune-visual-profile')||'balanced';}catch{}
 engine=createSculpture(host,{profile:savedProfile,onMode:(id,mode)=>{const caption=document.getElementById('cinema-shot');if(caption)caption.textContent=mode.title;const kicker=document.querySelector('.cinema-kicker');if(kicker)kicker.textContent=mode.subtitle;},onMetrics:m=>{host.dataset.fps=m.fps.toFixed(1);host.dataset.profile=m.profile;}});
 const unsubscribe=store.subscribe(state=>{engine.setState(state);const key=(state.job.active||state.phase==='completed')?state.job.filename:null;if(key===artifactKey&&(engine.geometryCount||inFlight||Date.now()-artifactAttempt<30000))return;artifactKey=key;artifactAttempt=Date.now();request?.abort();if(!key){engine.setGeometry(null);return;}request=new AbortController();inFlight=true;const expected=key;activeGeometry(state,request.signal).then(model=>{if(artifactKey===expected)engine.setGeometry(model);}).catch(()=>{}).finally(()=>{if(artifactKey===expected)inFlight=false;});});
 const transition=e=>{cinematic=e.detail.active;engine.setCinematic(cinematic);};window.addEventListener('hmi:cinematic',transition);
 const profileChange=e=>{if(e.key==='neptune-visual-profile')engine.setProfile(e.newValue);};window.addEventListener('storage',profileChange);
 host.dataset.renderer='ready';
 addEventListener('pagehide',event=>{if(event.persisted)return;request?.abort();unsubscribe();store.dispose();window.removeEventListener('hmi:cinematic',transition);window.removeEventListener('storage',profileChange);engine.dispose();});
}catch(error){host.dataset.renderer='unavailable';console.warn('Sculpture renderer unavailable',error.message);}
