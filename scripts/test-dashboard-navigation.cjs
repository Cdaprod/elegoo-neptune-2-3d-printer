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
    if (errors.length) throw new Error("Dashboard JavaScript runtime errors: " + errors.join("; "));
    process.stdout.write("All Observatory sidebar views activated in Chromium\n");
  } finally {
    await browser.close();
  }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
