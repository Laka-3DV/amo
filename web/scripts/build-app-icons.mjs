// Rasterize the vector source for iOS and manifest icons; no external services.
import { readFile, writeFile } from "node:fs/promises";
import { chromium } from "playwright";
const root = new URL("../public/", import.meta.url);
const svg = await readFile(new URL("favicon.svg", root), "utf8");
const browser = await chromium.launch();
try {
  for (const [name, size] of [["icon-192.png", 192], ["icon-512.png", 512],
    ["apple-touch-icon.png", 180], ["icon-maskable-512.png", 512]]) {
    const page = await browser.newPage({ viewport: { width: size, height: size }, deviceScaleFactor: 1 });
    await page.setContent(`<style>html,body{margin:0}svg{display:block;width:100vw;height:100vh}</style>${svg}`);
    await writeFile(new URL(name, root), await page.screenshot());
    await page.close();
  }
} finally { await browser.close(); }
