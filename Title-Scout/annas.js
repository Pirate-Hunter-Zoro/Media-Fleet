// Anna's Archive search via a real headless browser.
//
// Anna's Archive fronts its /search endpoint with a DDoS-Guard JavaScript challenge
// that a plain HTTP client (curl/urllib) cannot pass: it needs a real browser to run
// the fingerprint check, set the challenge cookie, and reload into the results page.
// (Its `.org`/`.se`/`.li`/`.gs` domains are suspended or parked as of 2026; the live
// mirrors are annas-archive.pk/.gd/.gl.)
//
// This script drives the system Chrome in *headed* mode over the Chrome DevTools
// Protocol (Chrome renders on the Mac's window server, so it looks like a real user
// browser to the challenge). Node 22+ ships a built-in WebSocket, so there are no npm
// dependencies. It is called by sources.search_annas_archive / sources.annas_download.
//
//   node annas.js search "title by author"   -> {"n":N,"results":[...]} on stdout
//   node annas.js slow <md5>                 -> {"title":..., "slow_urls":[...], ...}
//
// Environment:
//   AA_DOMAIN  override the mirror (default annas-archive.pk; the Python side tries
//              the mirrors in order).
//   CHROME_BIN  path to Chrome (default the standard macOS install).

import { spawn } from "node:child_process";
import { setTimeout as sleep } from "node:timers/promises";

const CHROME = process.env.CHROME_BIN ||
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const DOMAIN = process.env.AA_DOMAIN || "annas-archive.pk";
const MODE = process.argv[2] || "search";
const ARG = process.argv[3] || "";

async function getJson(url) {
  const r = await fetch(url);
  return r.json();
}

async function waitFor(fn, timeoutMs, intervalMs = 500) {
  const t0 = Date.now();
  for (;;) {
    const v = await fn();
    if (v) return v;
    if (Date.now() - t0 > timeoutMs) return null;
    await sleep(intervalMs);
  }
}

class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); }
  static async connect(url) {
    const ws = new WebSocket(url);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    const c = new CDP(ws);
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && c.pending.has(msg.id)) {
        const { resolve, reject } = c.pending.get(msg.id);
        c.pending.delete(msg.id);
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
      }
    };
    return c;
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
}

// Extract search-result rows from the rendered DOM. Each row is identified by its
// title link (a.js-vim-focus[href*="/md5/"]); the row also carries the author link,
// a font-mono source path (whose trailing ".pdf" gives the extension), and a metadata
// line ("English [en] · PDF · 91.7MB · 2009 · 📘 Book · 🚀/lgli/lgrs/...").
const SEARCH_EXTRACT = `(() => {
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a.js-vim-focus[href*="/md5/"]')) {
    const href = a.getAttribute('href') || '';
    const m = href.match(/\\/md5\\/([0-9a-f]{32})/);
    if (!m) continue;
    const md5 = m[1];
    if (seen.has(md5)) continue;
    seen.add(md5);
    const row = a.closest('div[class*="border-b"]') || a.parentElement.parentElement;
    const title = (a.textContent || '').trim();
    let author = '';
    const al = row ? row.querySelector('a[href^="/search?q="]') : null;
    if (al) author = (al.textContent || '').trim();
    let ext = '';
    const mono = row ? row.querySelector('div[class*="font-mono"]') : null;
    if (mono) {
      const src = (mono.textContent || '').trim();
      const em = src.match(/\\.([a-z0-9]{2,5})$/i);
      if (em) ext = em[1].toLowerCase();
    }
    let meta = '';
    const metaEl = row ? row.querySelector('div[class*="text-gray-800"]') : null;
    if (metaEl) {
      const clone = metaEl.cloneNode(true);
      clone.querySelectorAll('a').forEach((x) => x.remove());
      meta = (clone.textContent || '').replace(/\\s+/g, ' ').trim();
    }
    const parts = meta.split('·').map((s) => s.trim());
    const lang = (parts[0] || '').split(/[\\s[]/)[0];
    const size = parts.length > 2 ? parts[2] : '';
    let year = '';
    for (const p of parts) { const ym = p.match(/\\b(19|20)\\d{2}\\b/); if (ym) { year = ym[0]; break; } }
    let sources = '';
    for (const p of parts) { if (/lgli|lgrs|zlib|nexusstc|upload/i.test(p)) { sources = p; break; } }
    out.push({ md5, title, author, ext, size, lang, year, sources, meta, url: href });
  }
  return JSON.stringify({ title: document.title, url: location.href, n: out.length, results: out.slice(0, 25) });
})()`;

// Extract download links from a single-record page (/md5/<md5>).
const DETAIL_EXTRACT = `(() => {
  const title = document.title.replace(/^(.+?) \\- .*$/, '$1').trim();
  const slow = [...document.querySelectorAll('a.js-download-link[href*="/slow_download/"]')]
    .map((a) => a.getAttribute('href'));
  const libgen = [...document.querySelectorAll('a[href*="libgen."]')]
    .map((a) => a.getAttribute('href'))
    .filter((h) => /file\\.php|json\\.php|ads\\.php|get\\.php/.test(h));
  const ipfs = [...document.querySelectorAll('a[href^="ipfs://"]')]
    .map((a) => a.getAttribute('href'));
  return JSON.stringify({ title, slow_urls: slow, libgen_urls: libgen, ipfs });
})()`;

async function main() {
  const port = 9222 + Math.floor(Math.random() * 200);
  const chrome = spawn(CHROME, [
    "--no-first-run", "--no-default-browser-check", "--no-sandbox",
    `--user-data-dir=/tmp/annas_chrome_${port}`,
    `--remote-debugging-port=${port}`, "about:blank",
  ], { stdio: "ignore" });

  try {
    const version = await waitFor(async () => {
      try { return await getJson(`http://127.0.0.1:${port}/json/version`); }
      catch { return null; }
    }, 15000);
    if (!version) throw new Error("chrome devtools did not start");

    const targets = await getJson(`http://127.0.0.1:${port}/json/list`);
    const page = targets.find((t) => t.type === "page");
    const cdp = await CDP.connect(page.webSocketDebuggerUrl);
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");

    const targetUrl = MODE === "slow"
      ? `https://${DOMAIN}/md5/${ARG}`
      : `https://${DOMAIN}/search?q=${encodeURIComponent(ARG)}`;
    const extract = MODE === "slow" ? DETAIL_EXTRACT : SEARCH_EXTRACT;

    await cdp.send("Page.navigate", { url: targetUrl });

    let snapshot = null;
    await waitFor(async () => {
      const r = await cdp.send("Runtime.evaluate", { expression: extract, returnByValue: true });
      const v = r.result && r.result.value;
      if (typeof v === "string") {
        const parsed = JSON.parse(v);
        const ready = MODE === "slow" ? (parsed.slow_urls.length > 0 || parsed.libgen_urls.length > 0) : parsed.n > 0;
        if (ready) { snapshot = parsed; return true; }
        snapshot = parsed;
      }
      return null;
    }, 45000, 1000);

    console.log(JSON.stringify(snapshot || { n: 0, results: [], slow_urls: [], libgen_urls: [], ipfs: [] }));
    cdp.ws.close();
  } finally {
    chrome.kill("SIGKILL");
  }
}

main().catch((e) => { console.error("annas.js:", e.message); process.exit(1); });
