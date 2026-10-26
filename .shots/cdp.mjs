// 用 CDP 驱动 headless Edge 走完四步，逐步截图。
// 只用来看界面，不属于产品代码。
import fs from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";

const PORT = 9333;
const OUT = ".shots";

async function targets() {
  const r = await fetch(`http://127.0.0.1:${PORT}/json/list`);
  return r.json();
}

class CDP {
  constructor(url) {
    this.ws = new WebSocket(url);
    this.id = 0;
    this.pend = new Map();
    this.ready = new Promise((res) => (this.ws.onopen = res));
    this.ws.onmessage = (e) => {
      const m = JSON.parse(e.data);
      if (m.id && this.pend.has(m.id)) {
        this.pend.get(m.id)(m);
        this.pend.delete(m.id);
      }
    };
  }
  async send(method, params = {}) {
    await this.ready;
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((res) => this.pend.set(id, res));
  }
  async eval(expr) {
    const r = await this.send("Runtime.evaluate", {
      expression: expr, awaitPromise: true, returnByValue: true,
    });
    if (r.result?.exceptionDetails) {
      console.error("EVAL ERR:", JSON.stringify(r.result.exceptionDetails).slice(0, 400));
    }
    return r.result?.result?.value;
  }
  async shot(name) {
    const r = await this.send("Page.captureScreenshot", { format: "png" });
    fs.writeFileSync(`${OUT}/${name}.png`, Buffer.from(r.result.data, "base64"));
    console.log("shot", name);
  }
}

const list = await targets();
const page = list.find((t) => t.type === "page");
const c = new CDP(page.webSocketDebuggerUrl);
await c.send("Page.enable");
await c.send("Runtime.enable");
await c.send("Emulation.setDeviceMetricsOverride", {
  width: 1600, height: 1200, deviceScaleFactor: 1.6, mobile: false,
});

await c.send("Page.navigate", { url: "http://127.0.0.1:8848/" });
await sleep(1600);

// ① 填问题并点拆解
await c.eval(`(()=>{
  const ta=document.getElementById('q');
  ta.value='我想要做南京老门西历史文化街区的城市更新，想知道相关的文件';
  ta.dispatchEvent(new Event('input'));
  document.getElementById('go1').click();
  return 'ok';
})()`);
console.log("waiting plan...");
for (let i = 0; i < 60; i++) {
  await sleep(1000);
  const n = await c.eval(`document.querySelectorAll('#tasks .task').length`);
  if (n > 0) { console.log("plan tasks:", n); break; }
}
await sleep(600);
await c.shot("s2_plan");

// ② 取证
await c.eval(`document.getElementById('go2').click()`);
console.log("waiting gather...");
for (let i = 0; i < 60; i++) {
  await sleep(800);
  const n = await c.eval(`document.querySelectorAll('#results .item').length`);
  if (n > 0) { console.log("items:", n); break; }
}
await sleep(600);
await c.shot("s3_results");

// ③ 撰写
await c.eval(`document.getElementById('go3').click()`);
console.log("waiting write...");
for (let i = 0; i < 120; i++) {
  await sleep(1200);
  const done = await c.eval(`!!document.querySelector('#vbox .vbox')`);
  if (done) { console.log("verified"); break; }
}
await sleep(900);
const h = await c.eval(`document.body.scrollHeight`);
console.log("page height", h);
for (let i = 0; i < 4; i++) {
  await c.eval(`window.scrollTo(0, ${i * 700})`);
  await sleep(350);
  await c.shot("s4_" + i);
}
process.exit(0);
