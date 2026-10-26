// 验证 GitHub 上 README 的渲染：图片是否真的加载出来。
import fs from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";

const PORT = 9333;
const URL_ = "https://github.com/jingtingshanw/youju";

const t = await (await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" })).json();
const id = t.id;
const ws = new WebSocket(t.webSocketDebuggerUrl);
let seq = 0;
const pend = new Map();
const ready = new Promise((r) => (ws.onopen = r));
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
};
async function send(method, params = {}) {
  await ready;
  const n = ++seq;
  ws.send(JSON.stringify({ id: n, method, params }));
  return new Promise((r) => pend.set(n, r));
}
async function evalx(expr) {
  const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
  return r.result?.result?.value;
}

await send("Page.enable");
await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1200, height: 1600, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: URL_ });
await sleep(9000);

const title = await evalx(`document.title`);
console.log("页面标题:", title);

const imgs = await evalx(`JSON.stringify([...document.querySelectorAll('article img, .markdown-body img')].map(i => ({
  src: (i.currentSrc || i.src || '').split('/').slice(-2).join('/'),
  w: i.naturalWidth, h: i.naturalHeight
})))`);
let list = [];
try { list = JSON.parse(imgs || "[]"); } catch {}
console.log("README 里的图片:", list.length, "张");
for (const it of list) console.log(`  ${it.w > 0 ? "✓" : "✗"} ${it.w}×${it.h}  ${it.src}`);

const h1 = await evalx(`document.querySelector('article h1')?.innerText || '(无 h1)'`);
console.log("H1:", h1);
const heads = await evalx(`JSON.stringify([...document.querySelectorAll('article h2')].map(h => h.innerText))`);
console.log("二级标题:", heads);

const shot = await send("Page.captureScreenshot", { format: "png" });
fs.writeFileSync(".shots/github_render.png", Buffer.from(shot.result.data, "base64"));
console.log("截图 .shots/github_render.png");

await fetch(`http://127.0.0.1:${PORT}/json/close/${id}`);
process.exit(0);
