/* Simulated registry declarations, real fixture G-code geometry, no printer transport. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const crypto=require('node:crypto');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox','--enable-unsafe-swiftshader','--use-angle=swiftshader']});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:400},hasTouch:true});
  const base=process.env.HMI_BASE_URL||'http://127.0.0.1:8000',sha='b'.repeat(64),actions=[],writes=[],errors=[];
  const artifact={sha256:sha,filename:'Simulated-registry-fixture.gcode',archived:1,artifact_kind:'execution_gcode'};
  const revision={revision:{id:'revision',label:'Design A'},representations:[],authorities:[],components:[],parents:[]};let model=null;
  const geometry=await (await page.request.get(base+'/api/hmi/model-geometry?path=Display-bracket.gcode')).json();
  page.on('pageerror',e=>errors.push(e.message));
  page.on('request',r=>{if(r.url().includes('/api/')&&r.method()!=='GET')writes.push(new URL(r.url()).pathname);});
  page.on('dialog',dialog=>dialog.accept(dialog.type()==='prompt'?(dialog.message().includes('name')?'Simulated logical model':'Design A'):undefined));
  await page.route('**/api/model-registry/**',async route=>{
   const url=new URL(route.request().url()),path=url.pathname;
   if(path.endsWith('/actions')){
    const data=route.request().postDataJSON();actions.push(data);
    if(data.operation==='create_model'){model={id:'logical',name:data.name};return route.fulfill({json:model});}
    if(data.operation==='create_revision')return route.fulfill({json:revision.revision});
    if(data.operation==='representation')revision.representations=[{sha256:sha,role:data.role,active:1}];
    if(data.operation==='authority')revision.authorities=[{id:'authority',sha256:sha,role:data.role,state:data.approve?'approved':'candidate'}];
    return route.fulfill({json:{ok:true}});
   }
   if(path.endsWith('/models/logical'))return route.fulfill({json:{model,revisions:[revision]}});
   if(path.endsWith('/models'))return route.fulfill({json:{items:model?[{...model,preview:{...artifact,revision_id:'revision',label:'Design A'}}]:[],next:null}});
   if(path.endsWith('/artifacts'))return route.fulfill({json:{items:[artifact],next:url.searchParams.has('before')?null:'cursor'}});
   return route.fulfill({status:404,json:{detail:'Unavailable fixture resource'}});
  });
  await page.route('**/api/fabrication/artifacts/*/geometry',route=>route.fulfill({json:geometry}));
  await page.route('**/api/fabrication/artifacts/*/thumbnail*',route=>route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><path d="M10 60L40 10L70 60Z" fill="#64aac4"/></svg>'}));
  await page.goto(base+'/hmi');await page.locator('nav [data-go=models]').tap();await page.locator('#storage-provider').selectOption('unassigned');
  await page.locator('[aria-label="Next registry page"]').tap();await page.waitForFunction(()=>document.querySelector('[aria-label="Next registry page"]').disabled);
  await page.locator('[aria-label="Previous registry page"]').tap();
  await page.locator('#new-model').tap();await page.waitForFunction(()=>document.querySelector('#storage-provider').value==='models');
  await page.locator('#storage-provider').selectOption('unassigned');await page.locator('.model').first().tap();
  await page.locator('#model-viewer-host canvas').waitFor();await page.locator('#geometry-mode').selectOption('toolpath');
  await page.locator('#geometry-range').fill('50');await page.waitForFunction(()=>document.querySelector('#model-viewer-host').dataset.viewerPresentation==='toolpath'&&document.querySelector('#model-viewer-host').dataset.viewerRange==='0.5');
  await page.locator('#geometry-mode').selectOption('layers');await page.waitForFunction(()=>document.querySelector('#model-viewer-host').dataset.viewerPresentation==='layers');
  await page.locator('[data-view-angle=all]').tap();await page.waitForFunction(()=>document.querySelector('#model-viewer-host').dataset.viewerFraming==='all_paths');
  await page.locator('[data-view-angle=iso]').tap();await page.waitForFunction(()=>document.querySelector('#model-viewer-host').dataset.viewerFraming==='model');
  await page.locator('#model-tools summary').tap();
  await page.locator('#registry-candidate').tap();await page.locator('#registry-authorities').filter({hasText:'candidate'}).waitFor();
  await page.locator('#registry-approve').tap();await page.locator('#registry-authorities').filter({hasText:'approved'}).waitFor();
  assert.equal(actions.filter(a=>a.operation==='authority')[0].approve,false);
  assert.equal(actions.filter(a=>a.operation==='authority')[1].approve,true);
  const close=await page.locator('#inspector-close').boundingBox();assert.ok(close.y>=0&&close.y+close.height<=400,'Close must remain fully visible while declarations scroll');
  await page.locator('#inspector-close').tap();await page.locator('#storage-provider').selectOption('models');await page.locator('.model').filter({hasText:'Simulated logical model'}).waitFor();
  assert.equal(await page.locator('#model-viewer-host canvas').count(),0);
  assert.ok(writes.every(path=>path==='/api/model-registry/actions'));assert.deepEqual(errors,[]);
  // Real canonical HTTP integration, restricted to the isolated invalid-printer fixture.
  const status=await (await page.request.get(base+'/api/printer/status')).json();
  assert.ok(String(status.connection?.host||'').endsWith('.invalid'),'Fixture writes must never target a production printer');
  const fixture=JSON.parse(fs.readFileSync('/tmp/neptune-slicer-browser/manifest.json'));
  const ingest=async(name,kind)=>{const raw=fs.readFileSync('/tmp/neptune-slicer-browser/'+name);const response=await page.request.post(base+'/api/fabrication/ingest?filename='+encodeURIComponent(name)+'&artifact_kind='+kind,{headers:{'Content-Type':'application/octet-stream','X-Artifact-Source':'simulation:3mf-browser'},data:raw});assert.equal(response.status(),200);return (await response.json()).sha256;};
  assert.equal(await ingest('Simulated-Orca.3mf','slicer_project'),fixture.source_sha256);
  const mesh=await ingest('body.stl','manufacturing_geometry');for(const body of fixture.bodies)body.sha256=mesh;
  fixture.plates[0].sha256=await ingest('plate.stl','manufacturing_geometry');
  const registered=await page.request.post(base+'/api/fabrication/slicer-projects/'+fixture.source_sha256+'/register',{data:fixture});assert.equal(registered.status(),200);
  const registration=await registered.json();assert.equal(registration.bodies.length,2);assert.equal(registration.identity,'candidate');
  const repeated=await page.request.post(base+'/api/fabrication/slicer-projects/'+fixture.source_sha256+'/register',{data:fixture});assert.deepEqual(await repeated.json(),registration);
  const original=await page.request.get(base+'/api/fabrication/artifacts/'+fixture.source_sha256+'/original');assert.equal(crypto.createHash('sha256').update(await original.body()).digest('hex'),fixture.source_sha256);
  const actual=await browser.newPage({viewport:{width:1280,height:400},hasTouch:true});
  actual.on('pageerror',e=>errors.push(e.message));await actual.goto(base+'/hmi');await actual.locator('nav [data-go=models]').tap();await actual.locator('#storage-provider').selectOption('slicer_projects');
  await actual.locator('.model').filter({hasText:'Simulated-Orca.3mf'}).tap();await actual.locator('#model-viewer-host canvas').waitFor();
  await actual.locator('#slicer-detail summary').tap();await actual.locator('#slicer-metadata').filter({hasText:'2 extracted bodies'}).waitFor();
  if(process.env.HMI_SCREENSHOT_DIR)await actual.screenshot({path:process.env.HMI_SCREENSHOT_DIR+'/hmi-3mf-plate-1280x400.png'});
  await actual.locator('#inspector-close').tap();await actual.locator('#storage-provider').selectOption('models');await actual.locator('#model-filter').fill('Simulated 3MF body');
  await actual.locator('.model:not([hidden])').first().tap();await actual.locator('#model-viewer-host canvas').waitFor();
  await actual.locator('#inspector-details').filter({hasText:'3MF-declared units'}).waitFor();
  assert.equal((await (await page.request.get(base+'/api/fabrication/staging/queue')).json()).items.length,0);
  assert.deepEqual(errors,[]);
  console.log('Model reconciliation and actual canonical 3MF upload, body/plate geometry, idempotency, candidate identities and original-byte recovery passed');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
