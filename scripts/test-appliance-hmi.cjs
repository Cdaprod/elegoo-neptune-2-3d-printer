/* Integrated presentation regression: simulated data, real DOM/WebGL, no printer writes. */
const {chromium}=require('playwright');
const fs=require('node:fs');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const path=require('node:path');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox','--enable-unsafe-swiftshader','--use-angle=swiftshader']});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:400},hasTouch:true});
  const errors=[],writes=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('request',r=>{if(r.url().includes('/api/')&&!['GET','HEAD'].includes(r.method()))writes.push(r.url());});
  const plates=['Brackets','Crossbar','Saddle'].map((name,i)=>({name,output:`PART0${i+1}.gcode`,objects:[{id:'part',export:'part.stl'}]}));
  const artifacts=plates.map((p,i)=>({id:String(i+1).repeat(32),project:'browser-fixture',plate:p.name,fresh:true,staged:true,revision:'V004',checksum:'0'.repeat(64),receipt:{output:p.output}}));
  // Closed rectangular extrusion: actual STL WebGL rendering, no image substitution.
  let stl='solid browser_fixture\n';
  const v=[[0,0,0],[30,0,0],[30,8,0],[0,8,0],[0,0,40],[30,0,40],[30,8,40],[0,8,40]];
  for(const tri of [[0,2,1],[0,3,2],[4,5,6],[4,6,7],[0,1,5],[0,5,4],[1,2,6],[1,6,5],[2,3,7],[2,7,6],[3,0,4],[3,4,7]])stl+='facet normal 0 0 1\nouter loop\n'+tri.map(i=>'vertex '+v[i].join(' ')).join('\n')+'\nendloop\nendfacet\n';
  stl+='endsolid browser_fixture';
  await page.route('**/api/manufacturing/**',async route=>{
   const p=new URL(route.request().url()).pathname;
   if(p.endsWith('.stl'))return route.fulfill({body:stl});
   let body=[];
   if(p.endsWith('/projects'))body=[{name:'browser-fixture',repository:'Synthetic browser validation',geometryAvailable:true,plates,deployment:{autoBuild:true,autoSlice:true,autoStage:true}}];
   if(p.endsWith('/artifacts'))body=artifacts;
   if(p.endsWith('/worker'))body={online:true,state:'watching',delivery:{upload_enabled:false,sd_mounted:false}};
   return route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
  });
  await page.goto('http://127.0.0.1:8000/hmi');
  await page.locator('#hmi-ambience[data-renderer="ready"] canvas').waitFor();
  assert.equal(await page.locator('#hmi-ambience').evaluate(e=>getComputedStyle(e).pointerEvents),'none');
  const overviewDir=process.env.HMI_SCREENSHOT_DIR;
  if(overviewDir){fs.mkdirSync(overviewDir,{recursive:true});await page.screenshot({path:overviewDir+'/hmi-overview-1280x400.png'});}
  const overflow=()=>page.evaluate(()=>({x:document.documentElement.scrollWidth>innerWidth,y:document.documentElement.scrollHeight>innerHeight}));
  assert.deepEqual(await overflow(),{x:false,y:false});
  const cdp=await page.context().newCDPSession(page);
  async function swipe(x,y,dx,dy){
   await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x,y}]});
   for(let i=1;i<=6;i++)await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:x+dx*i/6,y:y+dy*i/6}]});
   await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
  }
  await swipe(700,180,-180,0);
  await page.locator('#view-print-bay.active').waitFor();
  const frame=page.frameLocator('#print-bay-frame');
  await frame.locator('.plate canvas').first().waitFor();
  await frame.getByText('Ready to send',{exact:true}).first().waitFor();
  await page.waitForTimeout(500);
  assert.equal(await frame.locator('body').evaluate(()=>document.documentElement.scrollHeight>innerHeight),false);
  assert.equal(await frame.locator('#deploy').isEnabled(),false);
  assert.equal(await frame.locator('#export-sd').isEnabled(),false);
  // Orbit must not select another plate or change the HMI route.
  const canvas=frame.locator('.plate canvas').nth(1),box=await canvas.boundingBox();
  await swipe(box.x+40,box.y+40,80,25);
  assert.equal(await frame.locator('.plate.selected').getAttribute('data-plate'),'Brackets');
  assert.equal(await page.locator('#view-print-bay.active').count(),1);
  const screenshots=process.env.HMI_SCREENSHOT_DIR;
  if(screenshots){fs.mkdirSync(screenshots,{recursive:true});await page.screenshot({path:screenshots+'/hmi-print-bay-1280x400.png'});}
  await page.locator('nav [data-go="models"]').tap();
  await page.locator('.model').first().waitFor();
  const first=page.locator('.model').first();
  // Exercise every real fixture thumbnail, including offscreen lazy images.
  // Wait for the route transition before decoding; do not swallow broken images.
  await page.locator('#view-models').evaluate(async panel=>{
   await Promise.all(panel.getAnimations().map(animation=>animation.finished));
  });
  assert.equal(await page.locator('.model').count(),5,'Expected all five local G-code fixtures');
  for(let i=0;i<5;i++){
   const model=page.locator('.model').nth(i);
   const img=model.locator('img');
   await img.scrollIntoViewIfNeeded();
   await img.evaluate(async image=>{
    image.loading='eager';
    try{await image.decode();}
    catch(error){throw new Error('Thumbnail decode failed: '+image.currentSrc+' ('+error.message+')');}
    if(!image.naturalWidth||!image.naturalHeight)throw new Error('Empty thumbnail: '+image.currentSrc);
   });
  }
  await page.locator('#models').evaluate(e=>e.scrollLeft=0);
  if(screenshots)await page.screenshot({path:screenshots+'/hmi-gallery-1280x400.png'});
  const before=await page.locator('#models').evaluate(e=>e.scrollLeft);
  await swipe(900,180,-220,0);
  assert.ok(await page.locator('#models').evaluate(e=>e.scrollLeft)>before);
  assert.equal(await page.locator('#view-models.active').count(),1);
  await page.locator('#models').evaluate(e=>e.scrollLeft=0);
  await first.tap();
  await page.locator('#model-viewer-host canvas').waitFor();
  await page.locator('#inspector-status').filter({hasText:'Interactive 3D'}).waitFor();
  await swipe(400,180,90,25);
  assert.equal(await page.locator('#model-inspector[open]').count(),1);
  await page.locator('#manage-model').tap();
  await page.locator('#view-controls.active').waitFor();
  await page.frameLocator('#operations-frame').locator('#view-sdcard.active').waitFor();
  assert.deepEqual(await overflow(),{x:false,y:false});
  await page.locator('nav [data-go="controls"]').tap();
  const operations=page.frameLocator('#operations-frame');
  await operations.locator('body.hmi-operations').waitFor();
  for(const name of ['machine','motion','sdcard','history','console']){
   await operations.locator(`.nav [data-view="${name}"]`).tap();
   await operations.locator(`#view-${name}.active`).waitFor();
  }
  assert.equal(await operations.locator('body').evaluate(()=>document.documentElement.scrollHeight>innerHeight),false);
  assert.deepEqual(writes,[]);assert.deepEqual(errors,[]);
  // Existing remote interfaces remain accessible and fit their respective widths.
  for(const viewport of [{width:1440,height:900},{width:390,height:844}]){
   await page.setViewportSize(viewport);await page.goto('http://127.0.0.1:8000/');
   assert.equal(await page.locator('#view-overview').isVisible(),true);
   assert.equal(await page.getByRole('link',{name:'Touch HMI',exact:true}).getAttribute('href'),'/hmi');
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  }
  const appDir=path.resolve(__dirname,'../integrations/mks-wifi');
  const authServer=spawn(path.join(appDir,'.venv/bin/neptune-observatory'),['--host','0.0.0.0','--port','8002'],{cwd:appDir,env:{...process.env,PYTHONPATH:'src',MKS_WIFI_HOST:'printer.example.invalid',NEPTUNE_OBSERVATORY_TOKEN:'browser-test-only',NEPTUNE_CONTROL_EXECUTION_ENABLED:'false',NEPTUNE_OCTOPRINT_CONTROLS_ENABLED:'false',NEPTUNE_MANUFACTURING_STORAGE:'/tmp/neptune-hmi-auth-test',NEPTUNE_MANUFACTURING_PROJECTS:'/tmp/neptune-hmi-auth-test/projects',NEPTUNE_TIMELINE_DB_PATH:'/tmp/neptune-hmi-auth-test.sqlite'},stdio:'ignore'});
  try{
   const authPage=await browser.newPage({viewport:{width:1280,height:400}});
   for(let i=0;i<60;i++){try{await authPage.goto('http://127.0.0.1:8002/hmi');break;}catch{await new Promise(r=>setTimeout(r,250));}}
   await authPage.locator('#login.visible').waitFor();
   assert.equal((await authPage.request.get('http://127.0.0.1:8002/api/printer/status')).status(),401);
   await authPage.getByLabel('Observatory access token',{exact:true}).fill('browser-test-only');
   await authPage.getByRole('button',{name:'Connect',exact:true}).click();
   await authPage.locator('#login.visible').waitFor({state:'hidden'});
   await authPage.locator('nav [data-go="print-bay"]').click();
   const authFrame=authPage.frameLocator('#print-bay-frame');
   await authFrame.locator('#login').waitFor({state:'hidden'});
   assert.equal((await authPage.request.get('http://127.0.0.1:8002/api/manufacturing/projects')).status(),200);
   await authPage.goto('http://127.0.0.1:8002/');
   assert.equal((await authPage.request.get('http://127.0.0.1:8002/api/printer/status')).status(),200);
   await authPage.goto('http://127.0.0.1:8002/hmi');
   await authPage.waitForTimeout(300);
   assert.equal(await authPage.locator('#login.visible').count(),0);
   await authPage.close();
  }finally{authServer.kill('SIGTERM');}
  console.log('Integrated 1280x400 touch, Print Bay, real G-code geometry, WebGL, overflow and remote-interface checks passed');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
