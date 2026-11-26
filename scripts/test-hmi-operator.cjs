/* Full HMI user journey, isolated fixtures and no printer writes. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox','--enable-unsafe-swiftshader','--use-angle=swiftshader']});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:400},hasTouch:true});
  const errors=[],writes=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('request',r=>{if(r.url().includes('/api/')&&!['GET','HEAD'].includes(r.method()))writes.push(r.method()+' '+r.url());});
  await page.goto('http://127.0.0.1:8000/hmi',{waitUntil:'domcontentloaded'});
  await page.locator('#hmi-ambience[data-renderer=ready] canvas').waitFor();
  for(const name of ['overview','print-bay','models','observations','controls','settings']){
   await page.locator('nav [data-go='+name+']').click();
   await page.locator('#view-'+name+'.active').waitFor();
   assert.equal(await page.locator('#view-'+name).isVisible(),true);
  }
  await page.locator('#hmi-profile').selectOption('low');
  await page.locator('#hmi-mode').selectOption('thermodynamics');
  await page.locator('#hmi-idle-timeout').selectOption('30');
  await page.locator('#hmi-idle-enabled').uncheck();
  await page.locator('#hmi-telemetry-effects').uncheck();
  await page.locator('#hmi-adaptive-quality').uncheck();
  const p=await page.evaluate(()=>JSON.parse(localStorage.getItem('neptune-hmi-preferences-v1')));
  assert.deepEqual(p,{enabled:false,idleSeconds:30,profile:'low',mode:'thermodynamics',telemetryEffects:false,adaptiveQuality:false});
  await page.reload();
  await page.locator('#hmi-ambience[data-renderer=ready] canvas').waitFor();
  await page.locator('nav [data-go=settings]').click();
  assert.equal(await page.locator('#hmi-profile').inputValue(),'low');
  assert.equal(await page.locator('#hmi-mode').inputValue(),'thermodynamics');
  assert.equal(await page.locator('#hmi-idle-enabled').isChecked(),false);
  await page.locator('#settings-preview').click();
  await page.locator('body[data-idle-phase=cinematic]').waitFor({timeout:7000});
  assert.equal(await page.locator('#app').evaluate(e=>e.inert),true);
  assert.equal(await page.locator('#hmi-ambience').getAttribute('data-profile'),'low');
  if(process.env.HMI_SCREENSHOT_DIR){fs.mkdirSync(process.env.HMI_SCREENSHOT_DIR,{recursive:true});await page.screenshot({path:process.env.HMI_SCREENSHOT_DIR+'/hmi-operator-studio-1280x400.png'});}
  await page.touchscreen.tap(80,360);
  await page.locator('body[data-idle-phase=active]').waitFor({timeout:7000});
  assert.equal(await page.locator('#view-settings.active').count(),1);
  await page.locator('#settings-reset').click();
  assert.equal(await page.locator('#hmi-profile').inputValue(),'balanced');
  await page.locator('nav [data-go=overview]').click();
  await page.locator('#home-cinema').click();
  await page.locator('body[data-idle-phase=cinematic]').waitFor({timeout:7000});
  await page.touchscreen.tap(80,360);
  await page.locator('body[data-idle-phase=active]').waitFor({timeout:7000});
  await page.locator('nav [data-go=models]').click();
  await page.locator('.model').first().waitFor();
  const total=await page.locator('.model').count();assert.ok(total>=5);
  await page.locator('#model-filter').fill('Saddle');
  assert.equal(await page.locator('.model:visible').count(),1);
  await page.locator('#model-filter').fill('');
  assert.equal(await page.locator('.model:visible').count(),total);
  await page.locator('nav [data-go=observations]').click();
  await page.waitForFunction(()=>document.getElementById('diag-fps')?.textContent!=='—');
  assert.ok(await page.locator('#diag-draw').textContent());
  assert.deepEqual(writes,[]);
  assert.deepEqual(errors,[]);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollHeight>innerHeight||document.documentElement.scrollWidth>innerWidth),false);
  console.log('Six HMI routes, persistent settings, manual preview, safe wake, gallery filtering and diagnostics passed');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
