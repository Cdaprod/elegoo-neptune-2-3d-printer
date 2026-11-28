/* Rendering regression: native-resolution studio and demand-driven 3D inspector. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');

(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox','--enable-unsafe-swiftshader','--use-angle=swiftshader']});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:400},hasTouch:true});
  const errors=[],writes=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.on('request',request=>{
   if(request.url().includes('/api/')&&!['GET','HEAD'].includes(request.method()))writes.push(request.url());
  });
  await page.route('**/api/printer/sdcard',r=>r.fulfill({
   contentType:'application/json',
   body:JSON.stringify({status:'available',entries:[],local_index:{
    status:'available',files:[{filename:'Profile-test.gcode',relative_path:'outputs/Profile-test.gcode',source_present:true,preview_available:false}]
   }})
  }));
  await page.route('**/api/hmi/model-preview?*',r=>r.fulfill({
   contentType:'image/svg+xml',
   body:'<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128"><rect width="128" height="128" fill="#1c334a"/></svg>'
  }));
  await page.route('**/api/hmi/model-geometry?*',r=>r.fulfill({
   contentType:'application/json',
   body:JSON.stringify({
    source:'reconstructed_gcode_extrusion',
    bounds:[[0,0,.2],[30,30,12]],
    segments:[[0,0,.2,30,0,.2,.44,0],[30,0,.2,30,30,.2,.44,0],[30,30,.2,0,30,.2,.44,0],[0,30,.2,0,0,.2,.44,0]],
    path_ranges:[{start:0,count:4,source_segments:4,closed:true,height:.2}],
    stats:{extrusion_segments:4,rendered_segments:4,sampling_stride:1,simplification:"connected_rdp_v1",max_deviation_mm:.036}
   })
  }));
  await page.goto('http://127.0.0.1:8000/hmi');
  await page.locator('#hmi-ambience[data-renderer=ready] canvas').waitFor();
  const studio=await page.locator('#hmi-ambience').evaluate(host=>{
   const canvas=host.querySelector('canvas');
   return {nativeWidth:canvas.width,cssWidth:canvas.clientWidth,filter:getComputedStyle(host).filter};
  });
  assert.ok(studio.nativeWidth>=studio.cssWidth,'Balanced studio must not downsample below native resolution');
  assert.ok(!studio.filter.includes('blur('),'Studio background must not have CSS blur');
  await page.locator('nav [data-go=models]').click();
  await page.locator('.model').first().click();
  await page.locator('#inspector-status').filter({hasText:'Interactive 3D'}).waitFor();
  const canvas=page.locator('#model-viewer-host canvas');
  await canvas.waitFor();
  const quality=await page.locator('#model-viewer-host').evaluate(host=>{
   const node=host.querySelector('canvas');
   return {width:node.width,cssWidth:node.clientWidth,pixelRatio:Number(host.dataset.viewerPixelRatio)};
  });
  assert.ok(quality.pixelRatio>=1&&quality.pixelRatio<=1.5,'Pixel ratio must be bounded');
  assert.ok(quality.width>quality.cssWidth,'Small model should supersample silhouette');
  assert.equal(await page.locator('#model-viewer-host').getAttribute('data-viewer-pipeline'),'connected_sweep');
  assert.ok(Number(await page.locator('#model-viewer-host').getAttribute('data-viewer-draw-calls'))<=5,'Connected surfaces must remain batched');
  await page.waitForFunction(()=>Number(document.getElementById('model-viewer-host')?.dataset.viewerFrames)>=2);
  await page.waitForTimeout(950);
  const before=Number(await page.locator('#model-viewer-host').getAttribute('data-viewer-frames'));
  await page.waitForTimeout(350);
  const after=Number(await page.locator('#model-viewer-host').getAttribute('data-viewer-frames'));
  assert.ok(after-before<=1,'Idle G-code viewer should not continuously redraw');
  await page.locator('#model-inspector [data-view-angle=top]').click();
  await page.waitForFunction(before=>Number(document.getElementById('model-viewer-host')?.dataset.viewerFrames)>before,before);
  await page.locator('#inspector-close').click();
  assert.equal(await page.locator('#model-viewer-host canvas').count(),0);
  // A touchscreen can emit pointerup without a compatibility click after WebGL gestures.
  // Both the model card and management action must still respond.
  await page.locator('.model').first().tap();
  await page.locator('#model-inspector[open]').waitFor();
  await page.locator('#manage-model').tap();
  await page.locator('#view-controls.active').waitFor();
  assert.deepEqual(writes,[]);
  assert.deepEqual(errors,[]);
  console.log('Three.js native studio resolution, crisp inspector, idle GPU suspension and controls passed');
 }finally{await browser.close();}
})().catch(err=>{console.error(err);process.exitCode=1;});
