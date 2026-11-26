import test from 'node:test';
import assert from 'node:assert/strict';
import {preparationState} from '../src/mks_wifi_bridge/static/print_bay_state.js';
const project={name:'mount',automation:{autoBuild:true,autoSlice:true,autoStage:true}};
const plate={name:'Brackets'};
const worker={online:true,state:'watching'};
const artifact={id:'ready',project:'mount',plate:'Brackets',fresh:true,staged:true};
const job=(status,result)=>({project:'mount',plate:'Brackets',status,result:JSON.stringify(result)});
test('prepared artifacts arrive ready without foreground build or stage actions',()=>{
  const state=preparationState(project,plate,[artifact],[],worker);
  assert.equal(state.kind,'ready');assert.equal(state.artifact.id,'ready');
});
test('stale staged artifacts are never delivery candidates while preparation runs',()=>{
  const state=preparationState(project,plate,[{...artifact,fresh:false}],
    [job('running',{phase:'Slicing plate'})],worker);
  assert.equal(state.kind,'preparing');assert.equal(state.label,'Slicing plate');assert.equal(state.artifact,undefined);
});
test('prepared revisions stay available when the build worker goes offline',()=>{
  const state=preparationState(project,plate,[artifact],[],{online:false});
  assert.equal(state.kind,'ready');assert.match(state.detail,/offline/);
});
test('worker absence is explicit rather than falsely promising preparation',()=>{
  const state=preparationState(project,plate,[],[job('queued',{})],{online:false});
  assert.equal(state.kind,'offline');assert.match(state.label,/Waiting/);
});
test('failed validation requires attention and never exposes a ready artifact',()=>{
  const state=preparationState(project,plate,[],[job('failed',{error:'Invalid BREP'})],worker);
  assert.equal(state.kind,'failed');assert.equal(state.detail,'Invalid BREP');assert.equal(state.artifact,undefined);
});
test('manual opt-out and paused watching are visible',()=>{
  assert.equal(preparationState({...project,automation:{autoBuild:false}},plate,[],[],worker).kind,'manual');
  assert.equal(preparationState(project,plate,[],[],{...worker,state:'manual'}).label,'Automatic preparation paused');
});
