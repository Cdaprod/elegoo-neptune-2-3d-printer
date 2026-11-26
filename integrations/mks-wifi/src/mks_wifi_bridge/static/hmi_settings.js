/* Operator-facing preferences, previews, and read-only renderer instrumentation. */
import {readPreferences,savePreferences,resetPreferences,subscribePreferences} from '/static/hmi_preferences.js';
const $=id=>document.getElementById(id);
const fields={enabled:'hmi-idle-enabled',idleSeconds:'hmi-idle-timeout',profile:'hmi-profile',mode:'hmi-mode',telemetryEffects:'hmi-telemetry-effects',adaptiveQuality:'hmi-adaptive-quality'};
const liveKeys=['enabled','telemetryEffects','adaptiveQuality'];
function show(next){
 for(const [key,id] of Object.entries(fields)){
  const el=$(id);if(!el)continue;if(liveKeys.includes(key))el.checked=next[key];else el.value=String(next[key]);
 }
 const status=$('settings-status');if(status)status.textContent='Preferences stored on this display · no printer commands';
}
subscribePreferences(show);
for(const [key,id] of Object.entries(fields)){
 const el=$(id);if(!el)continue;el.addEventListener('change',()=>{
  const value=liveKeys.includes(key)?el.checked:key==='idleSeconds'?Number(el.value):el.value;
  savePreferences({[key]:value});
 });
}
$('settings-reset')?.addEventListener('click',()=>resetPreferences());
function launch(mode){
 window.dispatchEvent(new CustomEvent('hmi:studio-preview',{detail:{mode:mode||readPreferences().mode}}));
}
$('home-cinema')?.addEventListener('click',()=>launch());
$('settings-preview')?.addEventListener('click',()=>launch());
$('home-settings')?.addEventListener('click',()=>document.querySelector('nav [data-go="settings"]')?.click());
for(const b of document.querySelectorAll('[data-preview-mode]')){
 b.addEventListener('click',()=>launch(b.dataset.previewMode));
}
$('model-filter')?.addEventListener('input',()=>window.dispatchEvent(new Event('hmi:filter-models')));
$('overview-observations')?.addEventListener('click',()=>document.querySelector('nav [data-go="observations"]')?.click());
const diagnostic=(id,v)=>{const el=$(id);if(el)el.textContent=v;};
window.addEventListener('hmi:renderer-metrics',event=>{
 const m=event.detail||{};
 diagnostic('studio-fps',Number.isFinite(m.fps)?m.fps.toFixed(0)+' FPS':'— FPS');
 diagnostic('diag-fps',Number.isFinite(m.fps)?m.fps.toFixed(1):'—');
 diagnostic('diag-frame',Number.isFinite(m.renderMs)?m.renderMs.toFixed(1)+' ms':'—');
 diagnostic('diag-scale',Number.isFinite(m.scale)?m.scale.toFixed(2)+'×':'—');
 diagnostic('diag-draw',Number.isFinite(m.drawCalls)?String(m.drawCalls):'—');
 diagnostic('diag-geometry',Number.isFinite(m.geometries)?String(m.geometries):'—');
 diagnostic('diag-textures',Number.isFinite(m.textures)?String(m.textures):'—');
 diagnostic('diag-mode',m.mode||'—');
 diagnostic('diag-profile',m.profile||'—');
});
window.addEventListener('hmi:telemetry',event=>{
 const s=event.detail||{},live=Boolean(s.connection?.connected)&&!s.stale;
 diagnostic('studio-phase',live?'LIVE TELEMETRY':s.stale?'STALE TELEMETRY':'SCHEMATIC / OFFLINE');
 diagnostic('bay-source',s.printer_backend==='octoprint'?'OctoPrint source':s.printer_backend==='mks'?'MKS source':'Backend unverified');
 diagnostic('controls-status',live?'Telemetry connected · controls retain operator gates':'Telemetry unavailable · command gates unchanged');
 diagnostic('diag-signal',live?'Live · '+(s.data_age_seconds??'—')+'s old':s.stale?'Stale':'Disconnected');
});
