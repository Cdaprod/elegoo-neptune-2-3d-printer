/* Browser-picked G-code uploads remain library-only and require explicit interaction. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');

(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox','--enable-unsafe-swiftshader','--use-angle=swiftshader']});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:400},hasTouch:true});
  const uploads=[],errors=[],otherWrites=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.on('request',request=>{
   if(request.url().includes('/api/')&&!['GET','HEAD'].includes(request.method())&&!request.url().includes('/api/hmi/import-gcode'))otherWrites.push(request.url());
  });
  await page.route('**/api/hmi/import-status',route=>route.fulfill({
   contentType:'application/json',
   body:JSON.stringify({available:true,configured:true,max_bytes:50*1024*1024,note:'Local-only file import'}),
  }));
  await page.route('**/api/hmi/import-gcode?filename=*',async route=>{
   const request=route.request();
   uploads.push({url:request.url(),method:request.method(),headers:request.headers(),data:request.postDataBuffer()});
   await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({
    status:'imported',filename:'Bracket.gcode',selected:false,printing:false,printer_command_sent:false,
   })});
  });
  await page.goto('http://127.0.0.1:8000/hmi');
  await page.locator('nav [data-go=models]').click();
  await page.waitForFunction(()=>document.getElementById('sd-import-status')?.textContent?.includes('Import available'));
  const file=Buffer.from('G90\nG1 X10 E1\n');
  await page.locator('#import-gcode-files').setInputFiles([{name:'Bracket.gcode',mimeType:'application/octet-stream',buffer:file}]);
  await page.waitForFunction(()=>document.getElementById('sd-import-status')?.textContent?.includes('Imported 1'));
  assert.equal(uploads.length,1);
  assert.equal(uploads[0].method,'POST');
  assert.equal(new URL(uploads[0].url).searchParams.get('filename'),'Bracket.gcode');
  assert.equal(uploads[0].headers['content-type'],'application/octet-stream');
  assert.deepEqual(uploads[0].data,file);
  assert.deepEqual(otherWrites,[]);
  assert.deepEqual(errors,[]);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollHeight>innerHeight||document.documentElement.scrollWidth>innerWidth),false);
  console.log('HMI import picker, authenticated local route, no printer commands, refresh and 1280x400 layout passed');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
