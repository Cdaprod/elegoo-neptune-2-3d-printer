/* Simulated registry declarations, real fixture G-code geometry, no printer transport. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
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
  await page.route('**/api/fabrication/artifacts/*/thumbnail',route=>route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><path d="M10 60L40 10L70 60Z" fill="#64aac4"/></svg>'}));
  await page.goto(base+'/hmi');await page.locator('nav [data-go=models]').tap();await page.locator('#storage-provider').selectOption('unassigned');
  await page.locator('[aria-label="Next registry page"]').tap();await page.waitForFunction(()=>document.querySelector('[aria-label="Next registry page"]').disabled);
  await page.locator('[aria-label="Previous registry page"]').tap();
  await page.locator('#new-model').tap();await page.waitForFunction(()=>document.querySelector('#storage-provider').value==='models');
  await page.locator('#storage-provider').selectOption('unassigned');await page.locator('.model').first().tap();
  await page.locator('#model-viewer-host canvas').waitFor();await page.locator('#geometry-mode').selectOption('toolpath');
  await page.locator('#geometry-range').fill('50');await page.waitForFunction(()=>document.querySelector('#model-viewer-host').dataset.viewerPresentation==='toolpath'&&document.querySelector('#model-viewer-host').dataset.viewerRange==='0.5');
  await page.locator('#geometry-mode').selectOption('layers');await page.waitForFunction(()=>document.querySelector('#model-viewer-host').dataset.viewerPresentation==='layers');
  await page.locator('#model-tools summary').tap();
  await page.locator('#registry-candidate').tap();await page.locator('#registry-authorities').filter({hasText:'candidate'}).waitFor();
  await page.locator('#registry-approve').tap();await page.locator('#registry-authorities').filter({hasText:'approved'}).waitFor();
  assert.equal(actions.filter(a=>a.operation==='authority')[0].approve,false);
  assert.equal(actions.filter(a=>a.operation==='authority')[1].approve,true);
  const close=await page.locator('#inspector-close').boundingBox();assert.ok(close.y>=0&&close.y+close.height<=400,'Close must remain fully visible while declarations scroll');
  await page.locator('#inspector-close').tap();await page.locator('#storage-provider').selectOption('models');await page.locator('.model').filter({hasText:'Simulated logical model'}).waitFor();
  assert.equal(await page.locator('#model-viewer-host canvas').count(),0);
  assert.ok(writes.every(path=>path==='/api/model-registry/actions'));assert.deepEqual(errors,[]);
  console.log('Model-centric paging, creation, representation reconciliation, explicit authority approval and bounded inspector passed');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
