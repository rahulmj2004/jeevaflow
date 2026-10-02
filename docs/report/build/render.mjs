// Markdown -> styled HTML -> PDF (headless Chrome).
//
//   BUILD_DEPS=<dir with node_modules containing marked + playwright-core> \
//   node render.mjs <report.md> <out.html> <out.pdf> [pages.json]
//
// pages.json (optional) maps section ids to page numbers for the TOC;
// build_pdf.py produces it from a first pass.

import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const require = createRequire(path.join(process.env.BUILD_DEPS ?? ".", "node_modules") + "/");
const { marked } = require("marked");
const { chromium } = require("playwright-core");

const [mdPath, htmlPath, pdfPath, pagesPath] = process.argv.slice(2);
const here = path.dirname(fileURLToPath(import.meta.url));
const reportDir = path.dirname(path.resolve(mdPath));
const css = fs.readFileSync(path.join(here, "report.css"), "utf8");
const pages = pagesPath && fs.existsSync(pagesPath) ? JSON.parse(fs.readFileSync(pagesPath, "utf8")) : {};

const slug = (s) => s.toLowerCase().replace(/<[^>]+>/g, "").replace(/&[a-z]+;/g, "").replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

let md = fs.readFileSync(mdPath, "utf8");
// The Markdown TOC is for readers of the .md file; the PDF gets a page-numbered one.
md = md.replace(/<!-- TOC:START -->[\s\S]*?<!-- TOC:END -->/, "<!--TOC-->");

let html = marked.parse(md, { gfm: true });

const sections = [];
html = html.replace(/<h2>(.*?)<\/h2>/g, (_, inner) => {
  const id = "sec-" + slug(inner);
  const m = /^(\d+)\.\s+(.*)$/.exec(inner);
  sections.push({ id, number: m ? m[1] : "", title: m ? m[2] : inner });
  const label = m ? `<span class="sec-num">${m[1]}</span><span class="sec-title">${m[2]}</span>` : `<span class="sec-title">${inner}</span>`;
  return `<h2 id="${id}" class="${m ? "numbered" : "unnumbered"}">${label}</h2>`;
});

const toc = `<section class="toc"><h1 class="toc-title">Contents</h1><ol>${sections
  .map(
    (s) =>
      `<li class="${s.number ? "" : "toc-un"}"><a href="#${s.id}"><span class="toc-n">${s.number ? s.number.padStart(2, "0") : ""}</span><span class="toc-t">${s.title}</span><span class="toc-dots"></span><span class="toc-p">${pages[s.id] ?? "00"}</span></a></li>`,
  )
  .join("")}</ol></section><div class="page-break"></div>`;

html = html.replace("<!--TOC-->", toc);
// Tag the first table of selected sections so CSS can style it alone.
for (const [id, cls] of [["sec-23-api-architecture", "api"], ["sec-26-security-threat-model", "threat"]]) {
  const at = html.indexOf(`<h2 id="${id}"`);
  const t = html.indexOf("<table>", at);
  if (at >= 0 && t >= 0) html = html.slice(0, t) + `<table class="${cls}">` + html.slice(t + 7);
}
// Figures: SVG diagrams get the figure frame.
html = html.replace(/<p><img src="(assets\/diagrams\/[^"]+)" alt="([^"]*)"><\/p>/g, '<figure class="diagram"><img src="$1" alt="$2"></figure>');

const page = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<base href="file://${reportDir}/">
<title>JeevaFlow — Healthcare Data Security &amp; Clinical Workflow Architecture Report</title>
<style>${css}</style></head><body>${html}</body></html>`;

fs.writeFileSync(htmlPath, page);
fs.writeFileSync(htmlPath.replace(/\.html$/, ".sections.json"), JSON.stringify(sections, null, 1));

const browser = await chromium.launch({ executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" });
const tab = await browser.newPage();
await tab.goto("file://" + path.resolve(htmlPath), { waitUntil: "load" });
await tab.evaluate(() => document.fonts.ready);
await tab.pdf({ path: pdfPath, preferCSSPageSize: true, printBackground: true, displayHeaderFooter: false });
await browser.close();
console.log("rendered", pdfPath, sections.length, "sections");
