// 给 README 截四张图：首页 / 依据清单 / 报告正文 / 核验面板。
// 只用于生成文档配图，不属于产品代码。
import fs from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";

const PORT = 9333;
const OUT = "docs/screenshots";
fs.mkdirSync(OUT, { recursive: true });

const QUESTION = "南京老门西历史文化街区做城市更新，涉及哪些政策和补偿标准";

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
      console.error("EVAL ERR:", JSON.stringify(r.result.exceptionDetails).slice(0, 300));
    }
    return r.result?.result?.value;
  }
  // 截指定元素；不传就用整个视口
  async shot(name, sel, maxH = 0) {
    let clip;
    if (sel) {
      const box = await this.eval(`(()=>{
        const el = document.querySelector(${JSON.stringify(sel)});
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return { x: r.x + window.scrollX, y: r.y + window.scrollY, w: r.width, h: r.height };
      })()`);
      if (!box) { console.error("没有这个元素:", sel); return; }
      const h = maxH ? Math.min(box.h, maxH) : box.h;
      clip = { x: Math.max(0, box.x - 12), y: Math.max(0, box.y - 12),
               width: Math.min(box.w + 24, 880), height: h + 24, scale: 1 };
    }
    const params = { format: "png" };
    if (clip) { params.clip = clip; params.captureBeyondViewport = true; }
    const r = await this.send("Page.captureScreenshot", params);
    const buf = Buffer.from(r.result.data, "base64");
    fs.writeFileSync(`${OUT}/${name}.png`, buf);
    console.log("  shot", name, Math.round(buf.length / 1024) + " KB");
  }
}

const list = await targets();
const page = list.find((t) => t.type === "page");
const c = new CDP(page.webSocketDebuggerUrl);
await c.send("Page.enable");
await c.send("Runtime.enable");
await c.send("Emulation.setDeviceMetricsOverride", {
  width: 880, height: 920, deviceScaleFactor: 2, mobile: false,
});
// 截图里不要出现滚动条
await c.send("Emulation.setScrollbarsHidden", { hidden: true }).catch(() => {});

await c.send("Page.navigate", { url: "http://127.0.0.1:8848/" });
await sleep(2200);

// 首页：等 coverage 数字填进来
const cov = await c.eval(`document.getElementById('cv-nat')?.textContent || ''`);
console.log("首页库规模数字:", JSON.stringify(cov));
await c.shot("01-home");

// 提问 → 拆解
await c.eval(`(()=>{
  const ta = document.getElementById('q');
  ta.value = ${JSON.stringify(QUESTION)};
  ta.dispatchEvent(new Event('input'));
  document.getElementById('go1').click();
  return 1;
})()`);
for (let i = 0; i < 60; i++) {
  await sleep(900);
  const n = await c.eval(`document.querySelectorAll('#tasks .task').length`);
  if (n > 0) { console.log("  拆解出", n, "个方向"); break; }
}
await sleep(700);

// 取证
await c.eval(`document.getElementById('go2').click()`);
let nItem = 0;
for (let i = 0; i < 90; i++) {
  await sleep(800);
  nItem = await c.eval(`document.querySelectorAll('#results .item').length`);
  if (nItem > 0) { console.log("  取到", nItem, "份文件"); break; }
}
await sleep(900);
const effs = await c.eval(`[...document.querySelectorAll('#results .t-dead,#results .t-pend,#results .t-ok')].map(e=>e.textContent).slice(0,8).join(' | ')`);
console.log("  时效标签:", JSON.stringify(effs || "(无)"));
await c.shot("02-sources", "#results", 620);

// 撰写
await c.eval(`document.getElementById('go3').click()`);
for (let i = 0; i < 150; i++) {
  await sleep(1200);
  const done = await c.eval(`!!document.querySelector('#vbox .vbox')`);
  if (done) { console.log("  核验面板已出"); break; }
}
await sleep(1200);
const vlen = await c.eval(`(document.getElementById('report')?.innerText || '').length`);
console.log("  报告字数:", vlen);
await c.shot("03-report", "#report", 880);
await c.shot("04-verify", "#vbox", 700);

// 拆解那一步要单独截：它在上一步就被替换掉了，所以重开一次页面
await c.send("Page.navigate", { url: "http://127.0.0.1:8848/" });
await sleep(2000);
await c.eval(`(()=>{
  const ta = document.getElementById('q');
  ta.value = ${JSON.stringify(QUESTION)};
  ta.dispatchEvent(new Event('input'));
  document.getElementById('go1').click();
  return 1;
})()`);
for (let i = 0; i < 60; i++) {
  await sleep(900);
  const n = await c.eval(`document.querySelectorAll('#tasks .task').length`);
  if (n > 0) { console.log("  重取拆解", n, "个方向"); break; }
}
await sleep(700);
await c.shot("06-plan", "#tasks", 700);

console.log("完成 →", OUT);
process.exit(0);
