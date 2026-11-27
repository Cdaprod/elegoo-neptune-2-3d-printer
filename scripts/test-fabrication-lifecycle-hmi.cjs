/* Explicitly simulated archive records; actual existing fixture geometry/WebGL. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const base=process.env.HMI_BASE_URL||'http://127.0.0.1:8000';
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox','--enable-unsafe-swiftshader','--use-angle=swiftshader']});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:400},hasTouch:true});
  const errors=[],writes=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('request',r=>{if(r.url().includes('/api/')&&r.method()!=='GET')writes.push(r.url());});
  const geometry=await (await page.request.get(base+'/api/hmi/model-geometry?path=Display-bracket.gcode')).json();
  assert.ok(geometry.stats.extrusion_segments>0);
  const image=await (await page.request.get(base+'/api/hmi/model-preview?path=Display-bracket.gcode')).body();
  await page.route('**/api/fabrication/**',async route=>{
   const url=new URL(route.request().url());const kind=url.pathname.split('/').at(-1);
   if(kind==='geometry')return route.fulfill({json:geometry});
   if(kind==='thumbnail')return route.fulfill({contentType:'image/svg+xml',body:image});
   const later=url.searchParams.has('before');
   const names=kind==='history'?(later?['Simulated older cancelled attempt']:['Simulated failed attempt','Simulated completed attempt']):kind==='unprinted'?['Simulated unprinted artifact']:['Simulated metadata-only SD file'];
   const items=names.map((name,i)=>({id:kind+i+(later?'old':''),filename:name+'.gcode',sha256:kind==='discovered'?null:'a'.repeat(64),bytes_available:kind!=='discovered',started_at:kind==='history'?'2026-01-02T10:00:00Z':undefined,ingested_at:'2026-01-01T10:00:00Z',state:kind==='history'?(later?'cancelled':i?'completed':'failed'):undefined,lifecycle:{discovery:'discovered',archive:kind==='discovered'?'metadata_only':'archived',preview:kind==='discovered'?'unavailable':'generated',delivery:'not_staged',execution:kind==='history'?'failed':'not_started',quality:'unassessed'}}));
   return route.fulfill({json:{schema_version:1,items,next:kind==='history'&&!later?'fixture-cursor':null}});
  });
  await page.goto(base+'/hmi');await page.locator('nav [data-go="models"]').tap();
  await page.locator('#storage-provider').selectOption('archive');
  await page.locator('.archive-lane').filter({has:page.locator('[aria-label="Print history Next page"]')}).waitFor();
  assert.equal(await page.locator('.archive-lane').count(),3);
  await page.locator('.archive-lane[data-collection="history"] .model').first().waitFor();
  await page.locator('[aria-label="Print history Next page"]').tap();
  await page.locator('.archive-lane[data-collection="history"]').filter({hasText:'older cancelled'}).waitFor();
  await page.locator('[aria-label="Print history Previous page"]').tap();
  await page.locator('.archive-lane[data-collection="history"]').filter({hasText:'failed attempt'}).waitFor();
  await page.locator(".archive-track img").evaluateAll(images=>Promise.all(images.map(async image=>{image.loading="eager";await image.decode();if(!image.naturalWidth)throw new Error("Empty chronological thumbnail");})));
  const output=process.env.HMI_SCREENSHOT_DIR;
  if(output){fs.mkdirSync(output,{recursive:true});await page.screenshot({path:output+'/lifecycle-chronology-1280x400.png'});}
  await page.locator('.archive-lane[data-collection="history"] .model').first().tap();
  await page.locator('#model-viewer-host canvas').waitFor();
  await page.locator('#lifecycle-details').filter({hasText:'failed'}).waitFor();
  if(output)await page.screenshot({path:output+'/lifecycle-inspector-1280x400.png'});
  await page.locator('#inspector-close').tap();
  assert.ok(await page.locator('.archive-lane[data-collection="history"] .model').first().evaluate(el=>el.classList.contains('selected')));
  assert.equal(await page.evaluate(()=>document.documentElement.scrollHeight>innerHeight),false);
  assert.deepEqual(writes,[]);assert.deepEqual(errors,[]);
  console.log('Three chronological lanes, independent paging, persistent selection, actual fixture geometry and no printer writes passed at 1280x400');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
