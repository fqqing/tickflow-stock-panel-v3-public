/**
 * S1 验收 —— 真浏览器跑个股终端的 KLinePro 多周期分钟K (CDP 驱动, 零依赖)
 *
 * 做什么:
 *   1. 打开 dev server 的个股终端, 打开 KLinePro 灰度开关(localStorage)
 *   2. 依次点 5分 / 30分 / 60分 / 日, 每次截图
 *   3. 收集 window.onerror 与 console.error, 断言没有运行时报错
 *   4. 读 DOM 断言周期工具条存在、canvas 已挂载
 *
 * 用法(Edge 已在 9222 就绪时):
 *   node run-minute-check.mjs [symbol]
 * 自带 Edge(同一次 Bash 调用内):
 *   msedge --headless=new --remote-debugging-port=9222 ... & sleep 3; node run-minute-check.mjs
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const SHOT_DIR = HERE
const SYMBOL = process.argv[2] || '600519.SH'
const BASE = 'http://127.0.0.1:3011'
const sleep = ms => new Promise(r => setTimeout(r, ms))

const list = await (await fetch('http://127.0.0.1:9222/json/list')).json()
let page = list.find(t => t.type === 'page' && t.webSocketDebuggerUrl)
if (!page) throw new Error('no page target: ' + JSON.stringify(list))
const ws = new WebSocket(page.webSocketDebuggerUrl)
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej })

let nextId = 1
const pending = new Map()
ws.onmessage = ev => {
  const msg = JSON.parse(ev.data)
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id) }
}
const send = (method, params = {}, sessionId) => new Promise(res => {
  const id = nextId++
  pending.set(id, res)
  ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }))
})
const call = (m, p = {}) => send(m, p)

async function evalJs(expr) {
  const r = await call('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })
  const rr = r.result
  if (rr && rr.exceptionDetails) return { __err: rr.exceptionDetails.text }
  return rr && rr.result ? rr.result.value : undefined
}

async function shot(name) {
  const s = await call('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true })
  if (s.result && s.result.data) {
    const p = path.join(SHOT_DIR, name)
    fs.writeFileSync(p, Buffer.from(s.result.data, 'base64'))
    return p
  }
  return null
}

await call('Page.enable')
await call('Runtime.enable')
// 页面级错误收集(必须在导航前注册)
await call('Page.addScriptToEvaluateOnNewDocument', {
  source: `
    window.__ERRS__ = []
    window.addEventListener('error', e => window.__ERRS__.push('onerror: ' + (e.message || '')))
    window.addEventListener('unhandledrejection', e => window.__ERRS__.push('reject: ' + String(e.reason)))
    const ce = console.error
    console.error = function (...a) { window.__ERRS__.push('console: ' + a.map(String).join(' ')); ce.apply(console, a) }
  `,
})
await call('Emulation.setDeviceMetricsOverride', { width: 1500, height: 900, deviceScaleFactor: 1, mobile: false })

// 1) 同源落地页 -> 打开 KLinePro 灰度开关
await call('Page.navigate', { url: BASE + '/' })
await sleep(1500)
await evalJs(`localStorage.setItem('tickflow.useKLinePro', 'true'); 'ok'`)

// 2) 进个股终端。vite dev 首次编译可能要十几秒, 轮询等 canvas 出现而不是死等
await call('Page.navigate', { url: `${BASE}/stock/${SYMBOL}` })
async function waitFor(expr, timeout = 40000, step = 700) {
  const t0 = Date.now()
  while (Date.now() - t0 < timeout) {
    const v = await evalJs(expr)
    if (v) return v
    await sleep(step)
  }
  return null
}
const got = await waitFor(`document.querySelectorAll('canvas').length > 0`)
if (!got) {
  console.log('!! 超时: 页面没有 canvas。诊断信息:')
  console.log(await evalJs(`JSON.stringify({
    href: location.href, title: document.title,
    rootLen: (document.getElementById('root') || {}).innerHTML?.length ?? -1,
    bodyText: document.body.innerText.slice(0, 400)
  })`))
}

const state = await evalJs(`JSON.stringify({
  url: location.pathname,
  canvas: document.querySelectorAll('canvas').length,
  tabs: [...document.querySelectorAll('button')].map(b => b.textContent.trim()).filter(t => ['日','周','月','1分','5分','15分','30分','60分'].includes(t)),
  klineBtn: [...document.querySelectorAll('button')].some(b => b.textContent.trim() === 'KLinePro'),
  bodyText: document.body.innerText.slice(0, 120)
})`)
console.log('页面状态:', state)

const results = []
for (const [label, file] of [['5分', 'minute-5m.png'], ['30分', 'minute-30m.png'], ['60分', 'minute-60m.png'], ['日', 'minute-day.png']]) {
  const clicked = await evalJs(`(() => {
    const b = [...document.querySelectorAll('button')].find(x => x.textContent.trim() === ${JSON.stringify(label)})
    if (!b) return false
    b.click()
    return true
  })()`)
  if (!clicked) { console.log(`按钮 ${label} 未找到, 跳过`); continue }
  await sleep(2500)
  const info = await evalJs(`JSON.stringify({
    errs: (window.__ERRS__ || []).slice(-6),
    canvas: document.querySelectorAll('canvas').length,
    src: [...document.querySelectorAll('span')].map(s => s.textContent.trim()).filter(t => /^(5m|30m|60m|1m)\\s*·/.test(t)).slice(-1)
  })`)
  const p = await shot(file)
  results.push({ label, file: p, info })
  console.log(`[${label}] canvas=${JSON.parse(info).canvas} src=${JSON.stringify(JSON.parse(info).src)} 错误=${JSON.stringify(JSON.parse(info).errs)}`)
  console.log('   截图:', p)
}

const finalErrs = await evalJs(`JSON.stringify((window.__ERRS__ || []).slice(0, 20))`)
console.log('累计错误:', finalErrs)
console.log(results.some(r => JSON.parse(r.info).errs.length) ? '总判定: NO-GO(有运行时错误)' : '总判定: GO(无运行时错误)')

ws.close()
await sleep(200)
process.exit(0)
