/* Simulated RSD API; actual fixture bytes and existing extrusion/WebGL renderer. No printer. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const crypto=require('node:crypto');
const base=process.env.HMI_BASE_URL||'http://127.0.0.1:8000';
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox','--enable-unsafe-swiftshader','--use-angle=swiftshader']});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:400},hasTouch:true});
  const errors=[],writes=[];let progress=0,complete=false;
  page.on('pageerror',error=>errors.push(error.message));
  page.on('request',request=>{if(request.url().includes('/api/')&&request.method()!=='GET')writes.push(new URL(request.url()).pathname);});
  const bytes=fs.readFileSync('/tmp/neptune-hmi-browser/Display-bracket.gcode');
  const sha=crypto.createHash('sha256').update(bytes).digest('hex');
  const asset={original_filename:'PART.GCO',name:'PART.GCO',provider:'printer_sd',sha256:sha,cache_available:true,geometry_available:true,size:bytes.length};
  const geometryResponse=await page.request.get(base+'/api/hmi/model-geometry?path=Display-bracket.gcode');
  assert.equal(geometryResponse.status(),200);
  const geometry=await geometryResponse.json();assert.ok(geometry.stats.extrusion_segments>0);
  const thumbnailResponse=await page.request.get(base+'/api/hmi/model-preview?path=Display-bracket.gcode');
  assert.equal(thumbnailResponse.status(),200);
  const thumbnail=await thumbnailResponse.body();
  await page.route('**/api/native-sd**',async route=>{
   const p=new URL(route.request().url()).pathname;
   let body;
   if(p==='/api/native-sd')body={status:'available',simulated:true,entries:[{name:'PART.GCO',provider:'printer_sd',retrievable:true,size:bytes.length}],assets:complete?[asset]:[]};
   else if(p==='/api/native-sd/transfers'&&route.request().method()==='POST'){
    assert.deepEqual(route.request().postDataJSON(),{filename:'PART.GCO'});progress=0;
    body={id:'fixture',status:'queued',received:0};
   }else if(p==='/api/native-sd/transfers/fixture'){
    complete=++progress>=3;body={id:'fixture',status:complete?'complete':'receiving',received:complete?bytes.length:progress*192,size:bytes.length,sha256:sha,asset};
   }else if(p.endsWith('/geometry'))body=geometry;
   else if(p.endsWith('/thumbnail')){
    return route.fulfill({contentType:'image/svg+xml',body:thumbnail});
   }else throw new Error('Unexpected native API request '+p);
   return route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
  });
  await page.route('**/api/factory-usb',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({available:true,selection_enabled:false,content_retrieval:false,observations:{}})}));
  await page.goto(base+'/hmi');
  await page.locator('nav [data-go="models"]').tap();
  await page.locator('.model[data-provider="printer_sd"]').waitFor();
  await page.locator('#storage-provider').selectOption('printer_sd');
  assert.equal(await page.locator('.model:visible').count(),1);
  await page.locator('.model:visible').tap();
  assert.equal(await page.locator('#model-viewer-host canvas').count(),0,'Directory names are not geometry');
  assert.ok(await page.locator('#inspector-empty').isVisible(),'Metadata-only SD entries need an honest empty state');
  assert.ok(await page.locator('#inspector-image').isHidden(),'No broken rendered-image claim without source bytes');
  assert.ok(await page.locator('[data-view-angle]').first().isDisabled());
  assert.ok(await page.locator('#factory-select-file').isVisible(),'Factory SD selection is part of the existing inspector');
  assert.ok(await page.locator('#factory-select-file').isDisabled(),'Uncommissioned factory SD selection stays gated');
  if(process.env.HMI_SCREENSHOT_DIR){fs.mkdirSync(process.env.HMI_SCREENSHOT_DIR,{recursive:true});await page.screenshot({path:process.env.HMI_SCREENSHOT_DIR+'/native-sd-metadata-1280x400.png'});}
  await page.locator('#retrieve-sd').tap();
  await page.locator('#native-sd-status').filter({hasText:'receiving'}).waitFor();
  await page.locator('#model-viewer-host canvas').waitFor();
  await page.locator('#inspector-status').filter({hasText:'Interactive 3D'}).waitFor();
  const screenshots=process.env.HMI_SCREENSHOT_DIR;
  if(screenshots){fs.mkdirSync(screenshots,{recursive:true});await page.screenshot({path:screenshots+'/native-sd-inspector-1280x400.png'});}
  await page.locator('#inspector-close').tap();
  await page.locator('#storage-provider').selectOption('native_cache');
  await page.locator('#refresh-files').tap();
  await page.locator('.model[data-provider="native_cache"]').waitFor();
  await page.locator('.model[data-provider="native_cache"] img').evaluate(async img=>{img.loading='eager';await img.decode();if(!img.naturalWidth)throw new Error('Empty native SD thumbnail');});
  if(screenshots)await page.screenshot({path:screenshots+'/native-sd-gallery-1280x400.png'});
  const overflow=await page.evaluate(()=>({x:document.documentElement.scrollWidth>innerWidth,y:document.documentElement.scrollHeight>innerHeight}));
  assert.deepEqual(overflow,{x:false,y:false});
  // Stress repeated viewer initialization, modal cleanup and navigation; keep the visible canvas assertion.
  for(let i=0;i<6;i++){
   await page.locator('.model[data-provider="native_cache"]').tap();
   await page.locator('#model-viewer-host canvas').waitFor();
   await page.locator('#inspector-status').filter({hasText:'Interactive 3D'}).waitFor();
   if(i===0)await page.locator('#manage-model').evaluate(button=>button.addEventListener('touchend',event=>event.preventDefault(),{once:true,capture:true,passive:false}));
   await page.locator('#manage-model').tap();
   await page.locator('#view-controls.active').waitFor();
   assert.equal(await page.locator('#model-viewer-host canvas').count(),0);
   await page.locator('nav [data-go="models"]').tap();
   await page.locator('.model[data-provider="native_cache"]').waitFor();
  }
  await page.unrouteAll({behavior:'wait'});
  assert.deepEqual(writes,['/api/native-sd/transfers']);assert.deepEqual(errors,[]);
  console.log('Simulated native SD discovery, progress, SHA identity, actual G-code geometry, touch filtering and six inspector lifecycle cycles passed at 1280x400');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
