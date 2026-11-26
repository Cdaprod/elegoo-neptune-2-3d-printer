/* Shared display-only settings. No credentials, machine writes or backend configuration. */
export const DEFAULT_PREFERENCES=Object.freeze({enabled:true,idleSeconds:150,profile:'balanced',mode:'auto',telemetryEffects:true,adaptiveQuality:true});
const KEY='neptune-hmi-preferences-v1';
const modes=['auto','ambient','architecture','thermodynamics','fabrication','intelligence'];
const times=[30,60,150,300,600];
function valid(raw){
 const r=raw&&typeof raw==='object'?raw:{};
 return {enabled:typeof r.enabled==='boolean'?r.enabled:true,
  idleSeconds:times.includes(r.idleSeconds)?r.idleSeconds:150,
  profile:['low','balanced','cinematic'].includes(r.profile)?r.profile:'balanced',
  mode:modes.includes(r.mode)?r.mode:'auto',
  telemetryEffects:typeof r.telemetryEffects==='boolean'?r.telemetryEffects:true,
  adaptiveQuality:typeof r.adaptiveQuality==='boolean'?r.adaptiveQuality:true};
}
export function readPreferences(){
 try{
  const stored=localStorage.getItem(KEY);
  if(stored)return valid(JSON.parse(stored));
  const legacy=localStorage.getItem('neptune-visual-profile');
  if(['low','balanced','cinematic'].includes(legacy))return {...DEFAULT_PREFERENCES,profile:legacy};
 }catch{}
 return {...DEFAULT_PREFERENCES};
}
export function savePreferences(patch){
 const next=valid({...readPreferences(),...patch});
 try{localStorage.setItem(KEY,JSON.stringify(next));}catch{}
 window.dispatchEvent(new CustomEvent('hmi:preferences',{detail:next}));
 return next;
}
export function resetPreferences(){
 try{localStorage.removeItem(KEY);localStorage.removeItem('neptune-visual-profile');}catch{}
 const next={...DEFAULT_PREFERENCES};window.dispatchEvent(new CustomEvent('hmi:preferences',{detail:next}));return next;
}
export function subscribePreferences(fn){
 const onChange=e=>fn(e.detail),onStorage=e=>{if(e.key===KEY||e.key===null)fn(readPreferences());};
 window.addEventListener('hmi:preferences',onChange);window.addEventListener('storage',onStorage);
 fn(readPreferences());
 return ()=>{window.removeEventListener('hmi:preferences',onChange);window.removeEventListener('storage',onStorage);};
}
