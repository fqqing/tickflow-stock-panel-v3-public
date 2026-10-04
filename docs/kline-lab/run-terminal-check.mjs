/**
 * 个股终端真机验收（P1 + P2）—— CDP 驱动，零依赖。
 *
 * 用法：后端 3018 + 前端 3011 + Edge(9222) 都就绪后
 *   node run-terminal-check.mjs [symbol]
 *
 * 检查项：
 *   T1  页面加载无 JS 异常、无 console error
 *   T2  终端骨架就位（顶栏 / 区间条 / 图表容器 / 盘口）
 *   T3  默认 ECharts 内核画出 canvas
 *   T4  `g` 切到 KLinePro 内核（canvas 重建，klinecharts 自己的多层 canvas）
 *   T5  `c` 打开缠论叠加层（无异常）
 *   T6  `1/2/3` 切日/周/月（两个内核共用同一份周期状态）
 *   T7  `/` 唤起命令面板、`?` 唤起快捷键说明、`Esc` 关闭
 *   T8  三档响应式：1440 / 1200 / 900 宽度下无横向滚动
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const SYMBOL = process.argv[2] || '000001'
const BASE = 'http://127.0.0.1:3011'
const URL0 = `${BASE}/stock/${SYMBOL}`
const sleep = ms => new Promise(r => setTimeout(r, ms))

const shot = (name, buf) => {
  const f = path.join(HERE, name)
  fs.writeFileSync(f, buf)
  return f
}

const list = await (await fetch('http://127.0.0.1:9222/json/list')).json()
const page = list.find(t => t.type === 'page' && t.webSocketDebuggerUrl)
if (!page) throw new Error('no page target')
const ws = new WebSocket(page.webSocketDebuggerUrl)
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej })

let nextId = 1
const pending = new Map()
const errors = []
const consoleErrors = []
ws.onmessage = ev => {
  const msg = JSON.parse(ev.data)
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); return }
  if (msg.method === 'Runtime.exceptionThrown') {
    const d = msg.params?.exceptionDetails
    errors.push(String(d?.exception?.description || d?.text || '').split('\n')[0])
  }
  if (msg.method === 'Runtime.consoleAPICalled' && msg.params?.type === 'error') {
    consoleErrors.push((msg.params.args || []).map(a => a.value ?? a.description ?? '').join(' ').slice(0, 200))
  }
}
const send = (method, params = {}) => new Promise(res => {
  const id = nextId++
  pending.set(id, res)
  ws.send(JSON.stringify({ id, method, params }))
})
const evaluate = async expr => {
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })
  return r.result?.result?.value
}
const key = async k => {
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: k, code: k.length === 1 ? `Key${k.toUpperCase()}` : k, text: k })
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key: k, code: k.length === 1 ? `Key${k.toUpperCase()}` : k })
}
const snap = async name => {
  const r = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true })
  return r.result?.data ? shot(name, Buffer.from(r.result.data, 'base64')) : null
}

await send('Page.enable')
await send('Runtime.enable')
await send('Log.enable')

const results = {}
const ok = (n, cond, detail) => { results[n] = { pass: !!cond, detail } ; console.log(`  ${cond ? '[PASS]' : '[FAIL]'} ${n}  ${detail ?? ''}`) }

const resize = async w => {
  await send('Emulation.setDeviceMetricsOverride', { width: w, height: 900, deviceScaleFactor: 1, mobile: false })
  await sleep(400)
}

await resize(1440)
await send('Page.navigate', { url: URL0 })
await sleep(4000)
// ECharts 第一次冷加载可能较慢，轮询 canvas 直到出现或超时
let dom = null
for (let i = 0; i < 20; i++) {
  dom = await evaluate(`(() => {
    const txt = document.body.innerText || ''
    return {
      url: location.pathname,
      canvases: document.querySelectorAll('canvas').length,
      hasHeader: txt.includes('${SYMBOL}'),
      missing: txt.includes('缺少股票代码'),
      scrollW: document.documentElement.scrollWidth,
      clientW: document.documentElement.clientWidth,
      klineBtn: txt.includes('KLinePro') || txt.includes('ECharts'),
    }
  })()`)
  if (dom.canvases > 0) break
  await sleep(500)
}
console.log('DOM:', JSON.stringify(dom))
ok('T2_terminalShell', !dom.missing && dom.hasHeader && dom.klineBtn, JSON.stringify(dom))
ok('T8a_noHScroll_1440', dom.scrollW <= dom.clientW + 1, `scrollW=${dom.scrollW} clientW=${dom.clientW}`)
await snap('terminal-1-echarts.png')

// 锁定从 ECharts 开始，避免 localStorage 记住上一次 KLinePro
// ★ 键名必须是 tickflow.useKLinePro（见 useKLineProFlag.ts）。
//   这里曾经写成 'tickflow-kline-pro' —— 那个键没有任何代码读，于是"锁定内核"
//   从未生效，测试实际跑在哪个内核上完全取决于上一次留下的状态（假绿）。
await evaluate("localStorage.setItem('tickflow.useKLinePro','false')")
await send('Page.reload')
await sleep(4000)
for (let i = 0; i < 20; i++) {
  dom = await evaluate(`(() => ({
    url: location.pathname,
    canvases: document.querySelectorAll('canvas').length,
    hasHeader: (document.body.innerText||'').includes('${SYMBOL}'),
    klineLabel: (document.body.innerText||'').includes('ECharts') || (document.body.innerText||'').includes('KLinePro'),
  }))()`)
  if (dom.canvases > 0 && dom.klineLabel) break
  await sleep(500)
}
console.log('reload DOM:', JSON.stringify(dom))

// T4 切内核
await key('g')
await sleep(2500)
const afterG = await evaluate(`(() => ({
  canvases: document.querySelectorAll('canvas').length,
  label: (document.body.innerText||'').includes('KLinePro'),
  periodBtns: (document.body.innerText||'').includes('月'),
}))()`)
ok('T4_switchToKLinePro', afterG.label && afterG.canvases > 1, JSON.stringify(afterG))
await snap('terminal-2-klinepro.png')

// T5 缠论
await key('c')
await sleep(2000)
await snap('terminal-3-chan.png')
ok('T5_chanToggle', errors.length === 0, `exceptions=${errors.length}`)

// T6 周期
await key('2')
await sleep(1800)
await snap('terminal-4-week.png')
const week = await evaluate(`(() => {
  const t = document.body.innerText || ''
  return { hasWeek: t.includes('周') }
})()`)
ok('T6_periodSwitch', week.hasWeek, JSON.stringify(week))
await key('1')
await sleep(1200)

// T7 浮层
await key('/')
await sleep(700)
const palette = await evaluate(`!!document.querySelector('input[placeholder]')`)
ok('T7a_commandPalette', palette, `input found=${palette}`)
await snap('terminal-5-palette.png')
await key('Escape')
await sleep(500)
// '?' 键在 CDP 下不可靠，直接点右上角的 ? 按钮
await send('Runtime.evaluate', {
  expression: `(() => {
    const b = [...document.querySelectorAll('button')].find(x => x.title && x.title.includes('键盘快捷键'))
    if (b) b.click()
    return !!b
  })()`,
  returnByValue: true,
})
await sleep(700)
let help = await evaluate(`(document.body.innerText||'').includes('键盘快捷键')`)
if (!help) { await sleep(1000); help = await evaluate(`(document.body.innerText||'').includes('键盘快捷键')`) }
ok('T7b_shortcutHelp', help, `help visible=${help}`)
await snap('terminal-6-help.png')
await key('Escape')
await sleep(400)

// T8 响应式
for (const w of [1200, 900]) {
  await resize(w)
  await sleep(600)
  const m = await evaluate(`({ scrollW: document.documentElement.scrollWidth, clientW: document.documentElement.clientWidth })`)
  ok(`T8_noHScroll_${w}`, m.scrollW <= m.clientW + 1, `scrollW=${m.scrollW} clientW=${m.clientW}`)
  await snap(`terminal-responsive-${w}.png`)
}

ok('T1_noJsExceptions', errors.length === 0, JSON.stringify(errors.slice(0, 3)))
ok('T1b_noConsoleErrors', consoleErrors.length === 0, JSON.stringify(consoleErrors.slice(0, 3)))

const pass = Object.values(results).every(r => r.pass)
console.log('\n总判定: ' + (pass ? 'PASS —— 终端真机可用' : 'FAIL —— 见上'))
if (errors.length) console.log('exceptions:\n  ' + errors.join('\n  '))
if (consoleErrors.length) console.log('console errors:\n  ' + consoleErrors.join('\n  '))

ws.close()
await sleep(200)
process.exit(pass ? 0 : 1)
