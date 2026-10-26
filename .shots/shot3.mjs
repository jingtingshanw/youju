// 拍结论卡区域（含核验摘要），视口按桌面标准。
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
await send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 1000, deviceScaleFactor: 1.5, mobile: false });
await sleep(400);
const y = await ev(`(()=>{const e=document.getElementById('verdict');if(!e)return -1;
  const r=e.getBoundingClientRect();return r.top+window.scrollY-170;})()`);
console.log("verdict y", y);
await ev(`window.scrollTo(0, ${Math.max(0, y)})`);
await sleep(600);
await shot("v_verdict");
process.exit(0);
