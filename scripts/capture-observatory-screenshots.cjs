/* Capture actual Observatory UI. Fixture history is explicitly labeled DEMO and no printer is contacted. */
const { chromium } = require("playwright");
const fs = require("node:fs");
const path = require("node:path");
const out = path.resolve("artifacts/observatory-screenshots");
fs.mkdirSync(out, { recursive: true });

const history = {
  source: "DEMO FIXTURE — OctoPrint PrintJobHistory (not real printer results)",
  total: 4, returned: 4, limit: 500,
  sessions: [
    { id:"demo-1",filename:"benchy.gcode",state:"success",outcome:"reported_completed",started_at:"2026-11-10 09:10",ended_at:"2026-11-10 10:32",duration_seconds:4920,progress:100,source:"OctoPrint PrintJobHistory plugin (DEMO)",material:"PLA",filament_weight_g:"13.8" },
    { id:"demo-2",filename:"raspberry-pi-enclosure.gcode",state:"success",outcome:"reported_completed",started_at:"2026-11-09 12:00",ended_at:"2026-11-09 15:14",duration_seconds:11640,progress:100,source:"OctoPrint PrintJobHistory plugin (DEMO)",material:"PETG",filament_weight_g:"45.2" },
    { id:"demo-3",filename:"calibration-cube.gcode",state:"cancel",outcome:"reported_cancelled",started_at:"2026-11-08 16:10",ended_at:"2026-11-08 16:18",duration_seconds:480,progress:24,source:"OctoPrint PrintJobHistory plugin (DEMO)",material:"PLA" },
    { id:"demo-4",filename:"bracket.gcode",state:"error",outcome:"reported_failed",started_at:"2026-11-07 11:00",ended_at:"2026-11-07 11:25",duration_seconds:1500,progress:47,source:"OctoPrint PrintJobHistory plugin (DEMO)",material:"PLA" },
  ],
};
async function main() {
  const browser = await chromium.launch({ headless:true, args:["--no-sandbox"] });
  try {
    const profiles = [
      {name:"desktop",viewport:{width:1440,height:960},deviceScaleFactor:1},
      {name:"mobile",viewport:{width:390,height:844},deviceScaleFactor:1,isMobile:true,hasTouch:true},
    ].filter(p => process.env.CAPTURE_VIEWPORT === "both" || !process.env.CAPTURE_VIEWPORT || process.env.CAPTURE_VIEWPORT === p.name);
    for (const profile of profiles) {
      const context = await browser.newContext({...profile, colorScheme:"dark"});
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", e => errors.push(e.message));
      await page.route("**/api/print/history/octoprint?*", async route => {
        await route.fulfill({status:200,contentType:"application/json",body:JSON.stringify(history)});
      });
      await page.goto("http://127.0.0.1:8000/",{waitUntil:"domcontentloaded"});
      await page.locator('button[data-view="history"]').click();
      await page.locator("#history-source").selectOption("octoprint");
      await page.waitForFunction(() => document.querySelector("#history-results")?.textContent?.includes("benchy.gcode"),null,{timeout:10000});
      await page.evaluate(() => {
        const banner = document.createElement("div");
        banner.textContent = "DEMO DATA • Real Observatory UI • No printer connected";
        Object.assign(banner.style,{position:"fixed",bottom:"0",left:"0",right:"0",background:"#9a3d18",color:"#fff",padding:"10px 20px",zIndex:"99999",font:"bold 13px system-ui",textAlign:"center"});
        document.body.appendChild(banner);
      });
      await page.screenshot({path:path.join(out,profile.name+"-print-history.png"),fullPage:true,animations:"disabled"});
      await page.locator('button[data-view="machine"]').click();
      await page.screenshot({path:path.join(out,profile.name+"-machine.png"),fullPage:true,animations:"disabled"});
      const overview = page.locator('button[data-view="overview"]');
      if (await overview.count()) {
        await overview.click();
        await page.screenshot({path:path.join(out,profile.name+"-overview.png"),fullPage:true,animations:"disabled"});
      }
      if (errors.length) console.warn(profile.name+" browser errors:",errors);
      await context.close();
    }
  } finally { await browser.close(); }
}
main().catch(err => {console.error(err);process.exitCode=1;});
