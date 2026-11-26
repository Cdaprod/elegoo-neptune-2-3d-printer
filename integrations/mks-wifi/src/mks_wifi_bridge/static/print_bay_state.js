// Operator-facing readiness. Stale artifacts never become delivery candidates.
export function preparationState(project, plate, artifacts, jobs, worker) {
  const revisions = artifacts.filter(a => a.project === project.name && a.plate === plate.name);
  const ready = revisions.find(a => a.fresh && a.staged);
  const automatic = ['autoBuild','autoSlice','autoStage'].every(k => project.automation?.[k] !== false);
  if (ready) return {kind:'ready', label:'Ready to send', artifact:ready, automatic,
    detail:worker.online?'Geometry validated · Sliced · Preview generated · Staged':'Prepared revision available offline'};
  const relevant = jobs.filter(j => j.project === project.name && j.plate === plate.name);
  const active = relevant.find(j => ['queued','running'].includes(j.status));
  if (active) {
    let result={};try{result=JSON.parse(active.result||'{}');}catch{}
    return {kind:worker.online?'preparing':'offline',label:worker.online?(result.phase||'Preparing automatically'):'Waiting for build worker',automatic,
      detail:worker.online?'This plate will be staged when preparation finishes.':'Preparation resumes when the build worker reconnects.'};
  }
  const latest = relevant[0];
  if (latest?.status === 'failed') {
    let result={};try{result=JSON.parse(latest.result||'{}');}catch{}
    return {kind:'failed',label:'Preparation needs attention',automatic,detail:result.error||'Preparation failed. Open build details for diagnostics.'};
  }
  if (!automatic || worker.state==='manual') return {kind:'manual',label:worker.state==='manual'?'Automatic preparation paused':'Automatic preparation disabled',automatic,
    detail:'Enable automatic build, slice and stage in this project’s manufacturing manifest.'};
  return {kind:worker.online?'waiting':'offline',label:worker.online?'Preparing automatically':'Waiting for build worker',automatic,
    detail:worker.online?'Waiting for source reconciliation or artifact synchronization.':'Prepared revisions remain available; new changes will be prepared when the build worker reconnects.'};
}
