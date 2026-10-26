// 检查夸克分享链接：里面有没有那个文件、要不要提取码。
// 只做验证，不属于产品代码。
import fs from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";

const PORT = 9333;
const URL_ = "https://pan.quark.cn/s/1858c1aafa53";

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
await send("Emulation.setDeviceMetricsOverride", { width: 1100, height: 800, deviceScaleFactor: 1.5, mobile: false });
await send("Page.navigate", { url: URL_ });
await sleep(7000);

const title = await evalx(`document.title`);
const text = await evalx(`(document.body.innerText || '').replace(/\\n{2,}/g, '\\n').slice(0, 900)`);
const hasFile = await evalx(`/pv-data/.test(document.body.innerText || '')`);
const needPw = await evalx(`/提取码|访问码|请输入/.test(document.body.innerText || '')`);

console.log("标题:", title);
console.log("页面里出现 pv-data:", hasFile);
console.log("页面要求提取码:", needPw);
console.log("---- 页面文字 ----");
console.log(text || "(空，可能是纯 canvas 或还在加载)");

const shot = await send("Page.captureScreenshot", { format: "png" });
fs.writeFileSync(".shots/quark_check.png", Buffer.from(shot.result.data, "base64"));
console.log("截图 .shots/quark_check.png");

await fetch(`http://127.0.0.1:${PORT}/json/close/${id}`);
process.exit(0);
