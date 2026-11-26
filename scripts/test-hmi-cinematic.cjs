/* Accelerated idle lifecycle, native wake input and rendering; no printer writes. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox','--enable-unsafe-swiftshader','--use-angle=swiftshader']});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:400},hasTouch:true});
  await page.clock.install();
  const errors=[],writes=[];page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.url().includes('/api/')&&!['GET','HEAD'].includes(r.method()))writes.push(r.url());});
  let telemetryFixture=null;await page.route('**/api/printer/status',r=>telemetryFixture?r.fulfill({contentType:'application/json',body:JSON.stringify(telemetryFixture)}):r.continue());
  await page.goto('http://127.0.0.1:8000/hmi');
  await page.locator('#hmi-ambience[data-renderer=ready] canvas').waitFor();
  const phase=()=>page.locator('body').getAttribute('data-idle-phase');
  await page.locator('#hmi-ambience canvas').evaluate(c=>c.dataset.identity='same-renderer');
  await page.clock.fastForward(140000);assert.equal(await phase(),'active');
  await page.locator('nav [data-go=controls]').tap();
  const frame=page.frameLocator('#operations-frame');await frame.locator('body.hmi-operations').waitFor();
  await page.clock.fastForward(140000);
  await frame.locator('.nav [data-view=sdcard]').tap();
  await page.clock.fastForward(140000);assert.equal(await phase(),'active','Iframe interaction must reset idle');
  await page.clock.fastForward(11000);await page.clock.runFor(2100);
  assert.equal(await phase(),'cinematic');assert.equal(await page.locator('#app').evaluate(e=>e.inert),true);
  assert.equal(await page.locator('#hmi-ambience canvas').getAttribute('data-identity'),'same-renderer');
  await page.locator('#app').evaluate(async e=>{await Promise.all(e.getAnimations().map(a=>a.finished));});
  if(process.env.HMI_SCREENSHOT_DIR){fs.mkdirSync(process.env.HMI_SCREENSHOT_DIR,{recursive:true});await page.screenshot({path:process.env.HMI_SCREENSHOT_DIR+'/hmi-cinematic-1280x400.png'});}
  await page.touchscreen.tap(90,360);assert.equal(await phase(),'waking');await page.clock.runFor(1400);
  assert.equal(await phase(),'active');assert.equal(await page.locator('#view-controls.active').count(),1,'Wake touch must not navigate underneath');
  const cdp=await page.context().newCDPSession(page);
  await page.clock.fastForward(150100);await page.clock.runFor(1900);
  await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:100,y:360}]});
  await page.clock.runFor(1600);assert.equal(await phase(),'waking','Held touch remains captured');
  await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});await page.clock.runFor(500);assert.equal(await phase(),'active');
  await page.clock.fastForward(150100);await page.clock.runFor(1900);
  await page.keyboard.down('Space');await page.clock.runFor(1300);assert.equal(await phase(),'waking');
  await page.keyboard.up('Space');await page.clock.runFor(500);assert.equal(await phase(),'active');
  await page.locator('#model-inspector').evaluate(d=>d.showModal());await page.clock.fastForward(151000);assert.equal(await phase(),'active','Dialogs suppress idle');
  await page.locator('#model-inspector').evaluate(d=>d.close());
  await page.clock.fastForward(151000);await page.clock.runFor(1900);
  telemetryFixture={connection:{connected:true},stale:false,telemetry:{printer_state:{value:'idle'}},print_job:{status:'unknown'}};
  await page.clock.fastForward(5100);await page.locator('#health[data-state=live]').waitFor();assert.equal(await phase(),'cinematic','Live telemetry updates must not reset idle');
  telemetryFixture={...telemetryFixture,connection:{connected:false},stale:true};
  await page.clock.fastForward(5100);await page.locator('#health[data-state=stale]').waitFor();await page.clock.runFor(1400);assert.equal(await phase(),'active','Connection loss wakes the workspace');
  await frame.locator('#sd-search').focus();await page.clock.fastForward(151000);assert.equal(await phase(),'active','Focused form suppresses idle');
  await frame.locator('#sd-search').blur();await page.clock.fastForward(151000);await page.clock.runFor(1900);
  await frame.locator('#auth-dialog').evaluate(d=>d.showModal());await page.clock.runFor(1400);assert.equal(await phase(),'active','New iframe dialog wakes HMI');
  await frame.locator('#auth-dialog').evaluate(d=>d.close());
  await page.emulateMedia({reducedMotion:'reduce'});await page.clock.fastForward(151000);await page.clock.runFor(20);assert.equal(await phase(),'cinematic');
  await page.touchscreen.tap(90,360);await page.clock.runFor(400);assert.equal(await phase(),'active');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollHeight>innerHeight||document.documentElement.scrollWidth>innerWidth),false);
  assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);
  console.log('Cinematic idle, iframe activity, continuous renderer, reduced motion, dialog holds and consumed touch/key wake passed');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
