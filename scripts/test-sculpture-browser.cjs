/* Real WebGL regression at the target viewport; all input is labeled simulation. */
const {chromium}=require('playwright');const assert=require('node:assert/strict');const fs=require('node:fs');
(async()=>{const browser=await chromium.launch({headless:true,args:['--no-sandbox','--enable-unsafe-swiftshader','--use-angle=swiftshader']});try{
 const page=await browser.newPage({viewport:{width:1280,height:400}}),errors=[],api=[];
 page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(m.type()==='error')errors.push(m.text());});page.on('request',r=>{if(r.url().includes('/api/'))api.push(r.method()+' '+r.url());});
 await page.goto('http://127.0.0.1:8000/visual-lab?mode=thermodynamics&fixture=printing&time=36');await page.waitForFunction(()=>window.sculptureLab?.engine.metrics.drawCalls>0);
 assert.match(await page.locator('#badge').textContent(),/SIMULATION/);assert.equal(await page.evaluate(()=>document.documentElement.scrollHeight>innerHeight||document.documentElement.scrollWidth>innerWidth),false);
 const heat=await page.evaluate(()=>sculptureLab.engine.uniforms.heat.value);await page.evaluate(()=>sculptureLab.setInput('nozzle_temperature',35));await page.waitForFunction(()=>sculptureLab.engine.uniforms.heat.value<.1);assert.ok(heat>.7);
 await page.evaluate(()=>sculptureLab.setFixture('missing'));assert.equal(await page.evaluate(()=>sculptureLab.state.fan.value),null);assert.equal(await page.evaluate(()=>sculptureLab.engine.geometryCount),0);
 for(const state of ['disconnected','idle','heating','printing','paused','cooling','completed','fault','stale']){await page.evaluate(s=>sculptureLab.setFixture(s),state);assert.equal(await page.evaluate(()=>sculptureLab.state.phase),state==='stale'?'unavailable':state);}
 for(const profile of ['low','balanced','cinematic']){await page.evaluate(p=>sculptureLab.setProfile(p),profile);await page.waitForFunction(p=>sculptureLab.engine.metrics.profile===p,profile);}
 await page.locator('#orbit').check();assert.equal(await page.locator('#orbit').isChecked(),true);await page.locator('#orbit').uncheck();
 const cleanup=await page.evaluate(()=>{const old=sculptureLab.engine;sculptureLab.recreate();return old.metrics;});assert.equal(cleanup.geometries,0);assert.equal(cleanup.textures,0);
 await page.waitForFunction(()=>sculptureLab.engine.metrics.drawCalls>0);assert.equal(await page.locator('#scene canvas').count(),1);assert.deepEqual(api,[],'Simulation must not contact printer APIs');
 const out=process.env.SCULPTURE_SCREENSHOT_DIR;
 for(const [mode,t,state] of [['ambient',0,'disconnected'],['architecture',18,'printing'],['thermodynamics',36,'printing'],['fabrication',60,'printing'],['intelligence',75,'heating']]){
  await page.goto(`http://127.0.0.1:8000/visual-lab?mode=${mode}&fixture=${state}&profile=cinematic&time=${t}&capture=1`);await page.waitForFunction(()=>window.sculptureLab?.engine.metrics.drawCalls>0);await page.waitForTimeout(350);
  assert.equal(await page.locator('#scene canvas').evaluate(c=>c.clientWidth),1280);if(out){fs.mkdirSync(out,{recursive:true});await page.screenshot({path:out+'/'+mode+'-1280x400.png'});}
 }
 assert.deepEqual(errors,[]);console.log('Five shader modes, telemetry uniforms, all state fixtures, profiles, missing data, 1280x400 previews and GPU resource cleanup passed');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
