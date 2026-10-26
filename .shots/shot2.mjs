// 首屏 + 核验区两次截图，中间走完整流程。
import fs from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";

const PORT = 9333, OUT = ".shots";
const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
const page = list.find(t => t.type === "page");
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0; const pend = new Map();
await new Promise(r => ws.onopen = r);
ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); } };
const send = (method, params = {}) => new Promise(res => { const i = ++id; pend.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
const ev = async x => (await send("Runtime.evaluate", { expression: x, awaitPromise: true, returnByValue: true })).result?.result?.value;
async function shot(name) {
  const r = await send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync(`${OUT}/${name}.png`, Buffer.from(r.result.data, "base64"));
  console.log("shot", name);
}

await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1100, deviceScaleFactor: 1.5, mobile: false });
await send("Page.navigate", { url: "http://127.0.0.1:8848/" });
await sleep(1600);
await shot("v_s1");

// 用示例 chip 填入并提交，顺便验证 chip 能点
await ev(`(()=>{document.querySelectorAll('.ex')[0].click();document.getElementById('go1').click();return 1;})()`);
for (let i = 0; i < 60; i++) { await sleep(1000); if (await ev(`document.querySelectorAll('#tasks .task').length`) > 0) break; }
await sleep(500);
await shot("v_s2");
await ev(`document.getElementById('go2').click()`);
for (let i = 0; i < 90; i++) { await sleep(1000); if (await ev(`document.querySelectorAll('#results .item').length`) > 0) break; }
await sleep(600);
await shot("v_s3");
await ev(`document.getElementById('go3').click()`);
for (let i = 0; i < 150; i++) { await sleep(1200); if (await ev(`!!document.querySelector('#vbox .vbox')`)) break; }
await sleep(900);
const y = await ev(`(()=>{const e=document.getElementById('vbox');if(!e)return -1;
  const r=e.getBoundingClientRect();return r.top+window.scrollY-70;})()`);
console.log("vbox y", y);
if (y > 0) { await ev(`window.scrollTo(0, ${y})`); await sleep(600); await shot("v_verify"); }
process.exit(0);
