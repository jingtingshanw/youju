import fs from "fs";
const CDP = "http://127.0.0.1:9333";
const list = await (await fetch(CDP + "/json/list")).json();
const page = list.find(t => t.type === "page" && t.url.includes("8848"));
if (!page) { console.log("no page", list.map(t => t.url)); process.exit(1); }

const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0; const pend = new Map();
const send = (method, params = {}) => new Promise(r => {
  const i = ++id; pend.set(i, r);
  ws.send(JSON.stringify({ id: i, method, params }));
});
await new Promise(r => ws.addEventListener("open", r));
ws.addEventListener("message", e => {
  const m = JSON.parse(e.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m.result); pend.delete(m.id); }
});

const ev = async expr => (await send("Runtime.evaluate",
  { expression: expr, returnByValue: true, awaitPromise: true })).result?.value;

const y = await ev(`(()=>{const h=[...document.querySelectorAll('.sec')].find(x=>/依据/.test(x.textContent.slice(0,4)));if(!h)return -1;return h.getBoundingClientRect().top+window.scrollY})()`);
console.log("依据段 y", y);
if (y > 0) {
  await ev(`window.scrollTo(0, ${Math.max(0, y - 40)})`);
  await new Promise(r => setTimeout(r, 700));
}
const shot = await send("Page.captureScreenshot", { format: "png" });
fs.writeFileSync(".shots/v_evidence.png", Buffer.from(shot.data, "base64"));
console.log("shot v_evidence");
ws.close();
