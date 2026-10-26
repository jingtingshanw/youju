// 只做一件事：打开首页，看有没有 JS 报错、四个数字有没有被 /v1/coverage 改写。
// 改完 index.html 就跑一次，比肉眼看代码快。
import fs from "node:fs";

const list = await (await fetch("http://127.0.0.1:9333/json/list")).json();
const page = list.find(t => t.type === "page");
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0; const pend = new Map();
const send = (method, params = {}) => new Promise(r => {
  const i = ++id; pend.set(i, r);
  ws.send(JSON.stringify({ id: i, method, params }));
});
await new Promise(r => ws.addEventListener("open", r));
const errs = [];
ws.addEventListener("message", e => {
  const m = JSON.parse(e.data);
  if (m.method === "Runtime.exceptionThrown")
    errs.push(JSON.stringify(m.params.exceptionDetails).slice(0, 300));
  if (m.method === "Log.entryAdded" && m.params.entry.level === "error")
    errs.push(String(m.params.entry.text).slice(0, 300));
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
});
const ev = async expr => (await send("Runtime.evaluate",
  { expression: expr, returnByValue: true, awaitPromise: true })).result?.result?.value;

await send("Runtime.enable");
await send("Log.enable");
await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride",
  { width: 1440, height: 1100, deviceScaleFactor: 2, mobile: false });
await send("Page.navigate", { url: "http://127.0.0.1:8848/" });
await new Promise(r => setTimeout(r, 2500));

console.log("JS 错误:", errs.length ? errs : "无");
for (const k of ["cv-nat", "cv-loc", "cv-cat", "cv-eff", "cv-exp"]) {
  console.log(" ", k, "=", await ev(`document.getElementById('${k}')?.textContent`));
}
const shot = await send("Page.captureScreenshot", { format: "png" });
fs.writeFileSync(".shots/s1_home.png", Buffer.from(shot.result.data, "base64"));
console.log("shot s1_home");
ws.close();
