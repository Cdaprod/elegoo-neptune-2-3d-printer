#!/usr/bin/env node
// Compile the inline dashboard script. The page must not silently ship broken JS.
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const file = path.join(__dirname, "../integrations/mks-wifi/src/mks_wifi_bridge/static/index.html");
const html = fs.readFileSync(file, "utf8");
const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)];
if (scripts.length !== 1) throw new Error("Expected exactly one inline dashboard script");
const match = scripts[0];
const line = html.slice(0, match.index).split("\n").length;
try {
  new vm.Script("\n".repeat(line - 1) + match[1], {filename:file});
  process.stdout.write("Dashboard JavaScript syntax valid\n");
} catch (error) {
  console.error(error.stack);
  process.exitCode = 1;
}
