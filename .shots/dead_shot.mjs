// 补一张图：让人一眼看到「同一批文件里，有一份已废止」。
// 先找到第一个失效标签，把那一屏滚到中间再截。
import fs from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";

const PORT = 9333;
const OUT = "docs/screenshots";
const QUESTION = "南京老门西历史文化街区做城市更新，涉及哪些政策和补偿标准";

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
await send("Emulation.setDeviceMetricsOverride", { width: 880, height: 920, deviceScaleFactor: 2, mobile: false });
await send("Emulation.setScrollbarsHidden", { hidden: true }).catch(() => {});
await send("Page.navigate", { url: "http://127.0.0.1:8848/" });
await sleep(2200);

await evalx(`(()=>{
  const ta = document.getElementById('q');
  ta.value = ${JSON.stringify(QUESTION)};
  ta.dispatchEvent(new Event('input'));
  document.getElementById('go1').click();
  return 1;
})()`);
for (let i = 0; i < 60; i++) {
  await sleep(900);
  if (await evalx(`document.querySelectorAll('#tasks .task').length`)) break;
}
await sleep(600);

await evalx(`document.getElementById('go2').click()`);
for (let i = 0; i < 90; i++) {
  await sleep(800);
  if (await evalx(`document.querySelectorAll('#results .item').length`)) break;
}
await sleep(1200);

const nDead = await evalx(`document.querySelectorAll('#results .t-dead').length`);
console.log("失效标签数:", nDead);

const info = await evalx(`(()=>{
  const tags = [...document.querySelectorAll('#results .t-dead')];
  if (!tags.length) return null;
  const t = tags[0];
  const item = t.closest('.item') || t.parentElement;
  item.scrollIntoView({ block: 'center' });
  const txt = item.innerText.split('\\n').filter(Boolean).slice(0, 3).join(' / ');
  return { tag: t.textContent.trim(), item: txt.slice(0, 90) };
})()`);
console.log("第一份失效件:", JSON.stringify(info));
await sleep(900);

const shot = await send("Page.captureScreenshot", { format: "png" });
fs.writeFileSync(`${OUT}/03-sources.png`, Buffer.from(shot.result.data, "base64"));
console.log("写出 03-sources.png");

await fetch(`http://127.0.0.1:${PORT}/json/close/${id}`);
process.exit(0);
