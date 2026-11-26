/* Browser-level regression: the sidebar must actually switch views even with no printer. */
const { chromium } = require("playwright");

async function main() {
  const browser = await chromium.launch({headless:true, args:["--no-sandbox"]});
  try {
    const page = await browser.newPage({viewport:{width:1280,height:800}});
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto("http://127.0.0.1:8000/", {waitUntil:"domcontentloaded"});
    for (const name of [
      "temperatures", "motion", "history", "machine", "sdcard",
      "diagnostics", "digital-twin", "console", "overview",
    ]) {
      await page.locator(`.nav button[data-view="${name}"]`).click();
      const selected = await page.locator(`#view-${name}`).evaluate(
        (element) => element.classList.contains("active") && getComputedStyle(element).display !== "none"
      );
      if (!selected) throw new Error(`Dashboard navigation did not activate: ${name}`);
    }
    // Print Bay must remain usable with no printer and a simulated build node.
    const queued=[];
    const stl='solid preview\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 10 0 0\nvertex 0 10 5\nendloop\nendfacet\nendsolid preview';
    await page.route("**/api/manufacturing/**", async route => {
      const request=route.request(), path=new URL(request.url()).pathname;
      if(path.endsWith('.stl'))return route.fulfill({status:200,body:stl});
      let body=[];
      if(path.endsWith('/worker'))body={online:true,state:'watching',delivery:{upload_enabled:false,sd_mounted:false}};
      if(path.endsWith('/artifacts'))body=[{id:'a'.repeat(32),project:'browser-fixture',plate:'Brackets',fresh:true,staged:true,revision:'V001',checksum:'0'.repeat(64),receipt:{output:'BRACKET.gcode'}}];
      if(path.endsWith('/projects'))body=[{name:'browser-fixture',repository:'test/project',geometryAvailable:true,plates:[{name:'Brackets',output:'BRACKET.gcode',objects:[{id:'left',export:'left.stl'}]}]}];
      if(request.method()==='POST'&&path.endsWith('/jobs')){queued.push(request.postDataJSON());body={job:'browser-test-job',status:'queued'};}
      await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
    });
    await page.goto("http://127.0.0.1:8000/print-bay",{waitUntil:'domcontentloaded'});
    await page.getByRole('button',{name:'Select Brackets',exact:true}).waitFor();
    await page.getByText('Ready to send',{exact:true}).first().waitFor();
    if(queued.length)throw new Error('Visiting Print Bay queued a foreground build');
    if(await page.getByRole('button',{name:'Rebuild & Slice',exact:true}).isVisible())throw new Error('Build controls are prominent in the ready view');
    await page.getByText('Preparation details & recovery',{exact:true}).click();
    await page.getByRole('button',{name:'Rebuild & Slice',exact:true}).click();
    if(queued.length!==1||queued[0].plate!=='Brackets'||queued[0].action!=='build-slice')throw new Error('Print Bay did not queue the selected plate');
    await page.setViewportSize({width:400,height:1280});
    if(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth))throw new Error('Print Bay portrait layout overflows');
    if(await page.getByRole('button',{name:'Upload to printer',exact:true}).isEnabled())throw new Error('Unconfigured printer upload was enabled');
    if (errors.length) throw new Error("Dashboard JavaScript runtime errors: " + errors.join("; "));
    process.stdout.write("All Observatory sidebar views activated in Chromium\n");
  } finally {
    await browser.close();
  }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
