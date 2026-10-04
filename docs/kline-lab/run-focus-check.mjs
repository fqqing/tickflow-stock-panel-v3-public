/**
 * P4-2 真机回归: 时间轴点击后的视口落点。
 *
 * ★ 为什么值得留着: KLinePro 的定位公式依赖一个**未写进文档**的行为 ——
 *   scrollToDataIndex(j) 是把 j 放在视口**右端**(实测 to ≈ j+2, 那 2 根是右侧
 *   留白), 不是左端。按直觉写成 idx - anchor*屏宽 会把目标推到视口左侧外面。
 *   升级 klinecharts 时这个行为可能变, 变了这里会红, 而不是让用户在某次升级后
 *   发现「点了时间轴跳过去, 目标却不在那儿」。
 *
 * 用法(Edge 已在 9222 就绪, 或同一次调用内先起 Edge):
 *   node run-focus-check.mjs
 *
 * 用 KLinePro 里那套公式去定位, 再读回真实 getVisibleRange() 算目标落在视口的
 * 相对位置, 断言 ≈ anchor。
 */
const PAGE = 'file:///D:/project/GP/tickflow-stock-panel/docs/kline-lab/focus-lab.html'
const sleep = ms => new Promise(r => setTimeout(r, ms))

const list = await (await fetch('http://127.0.0.1:9222/json/list')).json()
let page = list.find(t => t.type === 'page' && t.webSocketDebuggerUrl)
if (!page) throw new Error('no page target')
const ws = new WebSocket(page.webSocketDebuggerUrl)
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej })

let nextId = 1
const pending = new Map()
ws.onmessage = ev => {
  const msg = JSON.parse(ev.data)
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id) }
}
const send = (method, params = {}) => new Promise(res => {
  const id = nextId++
  pending.set(id, res)
  ws.send(JSON.stringify({ id, method, params }))
})

await send('Page.enable')
await send('Runtime.enable')
await send('Page.navigate', { url: PAGE })
await sleep(2000)

const evalJs = async (expr) => {
  const r = await send('Runtime.evaluate', {
    expression: expr, awaitPromise: true, returnByValue: true,
  })
  if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails))
  return r.result?.result?.value
}

for (let i = 0; i < 20; i += 1) {
  if (await evalJs('!!window.__ready')) break
  await sleep(300)
}
const N = await evalJs('window.__N')
console.log(`数据根数: ${N}`)

const out = await evalJs(`(async () => {
  const chart = window.__chart
  const sleep = ms => new Promise(r => setTimeout(r, ms))
  await sleep(1200)
  // 稳定视口: 先滚到中段, 避开刚加载时的偏窄视口
  chart.scrollToDataIndex(150, 0)
  await sleep(400)
  const r0 = chart.getVisibleRange()
  const vis = r0.to - r0.from + 1
  const anchor = 0.35
  const rows = []
  for (const idx of [10, 60, 120, 180, 240, 299]) {
    const j = Math.max(0, idx + Math.round((vis - 1) * (1 - anchor)) - 2)
    chart.scrollToDataIndex(j, 0)
    await sleep(350)
    const r = chart.getVisibleRange()
    const rel = (idx - r.from) / Math.max(1, r.to - r.from)
    rows.push({ idx, j, from: r.from, to: r.to, rel: +rel.toFixed(3) })
  }
  return { vis, rows, N: window.__N }
})()`)

console.log(`稳定视口宽度: ${out.vis}\n`)
let pass = 0
let fail = 0
const ok = (name, cond, extra = '') => {
  if (cond) { pass += 1; console.log(`  ok  ${name} ${extra}`) }
  else { fail += 1; console.log(`FAIL  ${name} ${extra}`) }
}

for (const r of out.rows) {
  const inView = r.idx >= r.from && r.idx <= r.to
  ok(`目标 ${r.idx} 在视口内`, inView, `(视口 ${r.from}~${r.to})`)
  if (r.idx >= out.vis && r.idx <= out.N - out.vis) {
    // 中段: 前后都有足够 bar, 落点应当贴合 anchor
    ok(`  目标 ${r.idx} 落在 anchor≈0.35 处`, Math.abs(r.rel - 0.35) <= 0.12, `(rel=${r.rel})`)
  } else {
    console.log(`  --  目标 ${r.idx} 靠边界, 只保证可见 (rel=${r.rel})`)
  }
}

console.log(`\n${pass} passed, ${fail} failed`)
await send('Browser.close')
process.exit(fail === 0 ? 0 : 1)
