import {normalize} from './state.js';
/* Shares existing authenticated snapshots/WebSocket; no command endpoint is used. */
export function createVisualStore({externalHistory=false}={}){
 let snapshot={},history=[],state=normalize(),socket=null,retry=null,disposed=false,controller=new AbortController();const listeners=new Set();
 function publish(next,rows=history){if(!next||typeof next!=='object')return;const old=Date.parse(snapshot.observed_at),stamp=Date.parse(next.observed_at);if(Number.isFinite(old)&&Number.isFinite(stamp)&&stamp<old)return;snapshot=next;history=rows;state=normalize(snapshot,history);for(const fn of listeners)fn(state);}
 const telemetry=e=>{publish(e.detail);if(!socket||socket.readyState>1)connect();},temperatures=e=>{history=e.detail.history||[];publish(snapshot,history);};
 window.addEventListener('hmi:telemetry',telemetry);window.addEventListener('hmi:temperatures',temperatures);
 async function initial(){try{const r=await fetch('/api/printer/status',{credentials:'same-origin',signal:controller.signal});if(r.ok)publish(await r.json());}catch{}}
 function connect(){if(disposed||document.hidden||(socket&&socket.readyState<2))return;socket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws/printer`);socket.onmessage=e=>{try{publish(JSON.parse(e.data));}catch{}};socket.onclose=e=>{if(!disposed&&e.code!==4401&&e.code!==4403)retry=setTimeout(connect,5000);};}
 async function refreshHistory(){if(disposed||document.hidden)return;try{const r=await fetch('/api/printer/temperatures',{credentials:'same-origin',signal:controller.signal});if(r.ok){history=(await r.json()).history||[];publish(snapshot,history);}}catch{}}
 const historyTimer=externalHistory?null:setInterval(refreshHistory,10000);if(!externalHistory)refreshHistory();
 const visibility=()=>{if(!document.hidden&&(!socket||socket.readyState>1))connect();};document.addEventListener('visibilitychange',visibility);initial();connect();
 return {get state(){return state;},subscribe(fn){listeners.add(fn);fn(state);return()=>listeners.delete(fn);},dispose(){disposed=true;controller.abort();clearTimeout(retry);clearInterval(historyTimer);socket?.close();listeners.clear();window.removeEventListener('hmi:telemetry',telemetry);window.removeEventListener('hmi:temperatures',temperatures);document.removeEventListener('visibilitychange',visibility);}};
}
export async function activeGeometry(state,signal){
 if(!state.job.active||!state.job.filename)return null;
 const response=await fetch('/api/printer/sdcard',{credentials:'same-origin',signal});if(!response.ok)return null;
 const listing=await response.json(),name=state.job.filename.toLowerCase(),base=name.split('/').at(-1);
 const files=(listing.local_index?.files||[]).filter(f=>f.source_present!==false&&(f.relative_path?.toLowerCase()===name||f.filename?.toLowerCase()===base));
 if(files.length!==1)return null;
 const r=await fetch('/api/hmi/model-geometry?path='+encodeURIComponent(files[0].relative_path),{credentials:'same-origin',signal});return r.ok?{...await r.json(),filename:state.job.filename}:null;
}
