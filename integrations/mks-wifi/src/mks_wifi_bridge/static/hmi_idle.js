import {readPreferences,subscribePreferences} from '/static/hmi_preferences.js';
/* Presentation-only idle lifecycle. No printer API or command path. */
export function createIdleExperience(win=window, idleMs=150000) {
 const doc=win.document, app=doc.getElementById('app'), overlay=doc.getElementById('idle-overlay');
 const reduced=()=>win.matchMedia('(prefers-reduced-motion: reduce)').matches;
 let preferences=readPreferences(),phase='active',timer,transition,lastActivity=Date.now(),releaseAt=0,disposed=false,restoreFocus=null;
 const pointers=new Set(),keys=new Set(),bound=new Map(),frameObservers=new Map();
 const clock=()=>Date.now(),configuredIdleMs=()=>idleMs!==150000?idleMs:preferences.idleSeconds*1000;
 function setPhase(value){phase=value;doc.body.dataset.idlePhase=value;win.dispatchEvent(new win.CustomEvent('hmi:cinematic',{detail:{active:value==='entering'||value==='cinematic',phase:value}}));}
 function schedule(delay=configuredIdleMs()){win.clearTimeout(timer);if(!preferences.enabled||disposed)return;timer=win.setTimeout(check,delay);}
 function visibleDocuments(){return [doc,...[...doc.querySelectorAll('iframe')].filter(f=>f.getBoundingClientRect().width&&f.getBoundingClientRect().height).map(f=>{try{return f.contentDocument;}catch{return null;}}).filter(Boolean)];}
 function blocked(){return visibleDocuments().some(d=>d.querySelector('dialog[open],#login.visible')||d.activeElement?.matches('input,textarea,select,[contenteditable="true"]'));}
 function check(){if(disposed||!preferences.enabled||phase!=='active')return;if(doc.hidden||blocked()||pointers.size||keys.size){lastActivity=clock();schedule(1000);return;}const remaining=configuredIdleMs()-(clock()-lastActivity);if(remaining>0){schedule(remaining);return;}enter();}
 function enter(){if(phase!=='active'||blocked())return;restoreFocus=doc.activeElement;overlay.hidden=false;overlay.setAttribute('aria-hidden','false');app.inert=true;setPhase('entering');transition=win.setTimeout(()=>{if(phase==='entering')setPhase('cinematic');},reduced()?0:1800);}
 function finishWake(){if(disposed)return;if(pointers.size||keys.size||clock()-releaseAt<300){transition=win.setTimeout(finishWake,100);return;}overlay.hidden=true;overlay.setAttribute('aria-hidden','true');app.inert=false;restoreFocus?.focus?.({preventScroll:true});setPhase('active');lastActivity=clock();schedule();}
 function wake(){if(phase==='active')return;win.clearTimeout(transition);setPhase('waking');transition=win.setTimeout(finishWake,reduced()?0:1000);}
 function activity(event){
  const moving=event.type==='pointermove';if(moving&&!event.movementX&&!event.movementY)return;
  if(event.type==='pointerup'||event.type==='pointercancel'){pointers.delete(event.pointerId);releaseAt=clock();}
  if(event.type==='keyup'){keys.delete(event.code);releaseAt=clock();}
  if(phase!=='active'){
   if(event.type==='pointerdown')pointers.add(event.pointerId);
   if(event.type==='keydown')keys.add(event.code);
   event.preventDefault();event.stopImmediatePropagation();if(phase!=='waking')wake();return;
  }
  if(event.type==='pointerdown')pointers.add(event.pointerId);if(event.type==='keydown')keys.add(event.code);
  lastActivity=clock();schedule();
 }
 const events=['pointerdown','pointerup','pointercancel','pointermove','click','keydown','keyup','wheel','input'];
 function bind(d){if(!d||bound.has(d))return;for(const event of events)d.addEventListener(event,activity,{capture:true,passive:false});bound.set(d,true);if(d!==doc){const observer=new win.MutationObserver(()=>{if(phase!=='active'&&blocked())wake();});observer.observe(d,{subtree:true,attributes:true,attributeFilter:['open','class']});frameObservers.set(d,observer);}}
 bind(doc);const frames=[...doc.querySelectorAll('iframe')];const loads=[];
 for(const frame of frames){let previous=null;const handler=()=>{try{const current=frame.contentDocument;if(previous&&previous!==current){for(const event of events)previous.removeEventListener(event,activity,true);bound.delete(previous);frameObservers.get(previous)?.disconnect();frameObservers.delete(previous);}bind(current);previous=current;}catch{}};frame.addEventListener('load',handler);loads.push([frame,handler]);handler();}
 function visibility(){if(doc.hidden){if(phase!=='active')wake();}else{lastActivity=clock();if(phase==='active')schedule();}}
 function interrupt(){wake();}
 doc.addEventListener('visibilitychange',visibility);win.addEventListener('hmi:interrupt',interrupt);
 // A newly opened authentication/confirmation dialog must remain visible.
 const observer=new win.MutationObserver(()=>{if(phase!=='active'&&blocked())wake();});
 observer.observe(doc,{subtree:true,attributes:true,attributeFilter:['open','class']});
 const preview=()=>{if(!disposed&&phase==='active'&&!blocked())enter();};
 win.addEventListener('hmi:studio-preview',preview);
 const stopPreferences=subscribePreferences(next=>{preferences=next;if(phase==='active'){lastActivity=clock();schedule();}else if(!next.enabled)wake();});
 setPhase('active');schedule();
 return {get phase(){return phase;},get snapshot(){return {phase,idleFor:clock()-lastActivity,heldPointers:pointers.size,heldKeys:keys.size,blocked:blocked(),hidden:doc.hidden};},dispose(){disposed=true;win.clearTimeout(timer);win.clearTimeout(transition);observer.disconnect();frameObservers.forEach(o=>o.disconnect());for(const d of bound.keys())for(const e of events)d.removeEventListener(e,activity,true);for(const [f,h] of loads)f.removeEventListener('load',h);doc.removeEventListener('visibilitychange',visibility);win.removeEventListener('hmi:interrupt',interrupt);win.removeEventListener('hmi:studio-preview',preview);stopPreferences();app.inert=false;overlay.hidden=true;overlay.setAttribute('aria-hidden','true');setPhase('active');}};
}
export const idleExperience=typeof window!=='undefined'&&document.getElementById('idle-overlay')?createIdleExperience():null;
