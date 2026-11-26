/* Browser regression: actual 1280x400 layout, 3D thumbnails, selection, no G-code dispatch. */
const { chromium } = require("playwright");

async function main() {
  const browser = await chromium.launch({
    headless:true,
    args:["--no-sandbox", "--enable-unsafe-swiftshader", "--use-angle=swiftshader"]
  });
  try {
    const page = await browser.newPage({
      viewport:{width:1280,height:400},hasTouch:true,isMobile:false
    });
    const errors = [];
    const unexpectedWrites = [];
    page.on("pageerror",error=>errors.push(error.message));
    page.on("request",request=>{
      if(request.url().includes("/api/") && !["GET","HEAD"].includes(request.method())) {
        unexpectedWrites.push(request.method()+" "+request.url());
      }
    });
    const names = [
      "Display-mount-V017.gcode","Voronoi-enclosure.gcode",
      "Camera-arm.gcode","Extrusion-saddle.gcode",
      "Pendant-lid.gcode","Bearing-bracket.gcode","Frame-clip.gcode"
    ];
    const files = names.map((name,i)=>({
      relative_path:"outputs/"+name,filename:name,
      source_present:true,preview_available:false,
      metadata:{estimated_print_time:(i+1)+"h 20m"}
    }));
    await page.route("**/api/printer/sdcard",async route=>{
      await route.fulfill({status:200,contentType:"application/json",body:JSON.stringify({
        status:"available",entries:[],local_index:{status:"available",files}
      })});
    });
    await page.route("**/api/hmi/model-preview?*",async route=>{
      const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="960" height="720">'+
        '<rect width="960" height="720" fill="#132a3d"/>'+
        '<path d="M220 420 L480 200 L740 420 L480 610 Z M220 420 V320 L480 110 L740 320 V420" '+
        'fill="#668aa6" stroke="#d8edf9" stroke-width="19"/>'+
        '<path d="M220 320 L480 510 L740 320" fill="none" stroke="#9fc2db" stroke-width="15"/>'+
        '</svg>';
      await route.fulfill({status:200,contentType:"image/svg+xml",body:svg});
    });
    await page.route("**/api/hmi/model-geometry?*",async route=>{
      await route.fulfill({status:200,contentType:"application/json",body:JSON.stringify({
        source:"reconstructed_gcode_extrusion",
        bounds:[[0,0,0.2],[20,20,20]],
        segments:[[0,0,0.2,20,0,0.2,.44,0],
          [20,0,.2,20,20,.2,.44,0],[20,20,.2,0,20,.2,.44,0],
          [0,20,.2,0,0,.2,.44,0]],
        stats:{extrusion_segments:4,rendered_segments:4,sampling_stride:1}
      })});
    });
    await page.goto("http://127.0.0.1:8000/hmi",{waitUntil:"domcontentloaded"});
    await page.locator('nav [data-go="models"]').click();
    await page.locator(".model").first().waitFor();
    const width=await page.locator("#app").evaluate(el=>Math.round(el.getBoundingClientRect().width));
    const height=await page.locator("#app").evaluate(el=>Math.round(el.getBoundingClientRect().height));
    if(width!==1280||height!==400)throw Error("Unexpected HMI geometry "+width+"x"+height);
    if(await page.locator(".model").count()!==names.length)throw Error("Model count incorrect");
    const isScrollable=await page.locator("#models").evaluate(el=>el.scrollWidth>el.clientWidth);
    if(!isScrollable)throw Error("Model gallery does not scroll horizontally");
    const first=page.locator(".model").first();
    if(!await first.locator("img").getAttribute("src").then(v=>v.includes("/api/hmi/model-preview"))) {
      throw Error("Gallery did not prefer reconstructed 3D preview");
    }
    await first.click();
    await page.locator("#model-inspector[open]").waitFor();
    if(!await page.locator("#inspector-title").textContent().then(s=>s.includes("Display-mount"))) {
      throw Error("3D inspector wrong model");
    }
    await page.locator('#model-inspector [data-view-angle="top"]').click();
    await page.locator("#inspector-close").click();
    if(await page.locator("#model-inspector[open]").count())throw Error("Inspector did not close");
    if(unexpectedWrites.length)throw Error("Unexpected printer API writes: "+unexpectedWrites.join(", "));
    if(errors.length)throw Error("JavaScript errors: "+errors.join("; "));
    if(process.env.HMI_SCREENSHOT)await page.screenshot({path:process.env.HMI_SCREENSHOT});
    console.log("G-code 3D gallery, model inspector, scroll layout and read-only browser checks passed.");
  } finally {
    await browser.close();
  }
}
main().catch(err=>{console.error(err);process.exitCode=1;});
