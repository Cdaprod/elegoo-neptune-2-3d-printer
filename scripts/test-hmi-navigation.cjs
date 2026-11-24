/* Read-only Waveshare 1280x400 kiosk smoke test. Requires a running Observatory and Playwright. */
const { chromium } = require("playwright");
(async () => {
 const browser = await chromium.launch({headless:true,args:["--no-sandbox"]});
 try {
  const page = await browser.newPage({viewport:{width:1280,height:400},hasTouch:true,isMobile:false});
  const errors=[];page.on("pageerror",e=>errors.push(e.message));
  await page.goto("http://127.0.0.1:8000/hmi",{waitUntil:"domcontentloaded"});
  await page.locator("#view-overview").waitFor();
  const metrics=await page.locator("#app").evaluate(e=>({w:e.getBoundingClientRect().width,h:e.getBoundingClientRect().height}));
  if(metrics.w!==1280||metrics.h!==400)throw Error("Unexpected HMI viewport "+JSON.stringify(metrics));
  for(const name of ["models","observations","controls","overview"]) {
    await page.locator('nav [data-go="'+name+'"]').click();
    const ok=await page.locator("#view-"+name).evaluate(e=>getComputedStyle(e).display!=="none");
    if(!ok)throw Error("Cannot navigate to "+name);
  }
  const desktop=await page.goto("http://127.0.0.1:8000/",{waitUntil:"domcontentloaded"});
  if(desktop.status()!==200)throw Error("Desktop Observatory is missing");
  if(errors.length)throw Error(errors.join("; "));
  console.log("Waveshare HMI and existing Observatory navigation smoke test passed");
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
