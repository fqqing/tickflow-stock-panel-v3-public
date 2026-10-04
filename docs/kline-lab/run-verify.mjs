/**
 * KLineChart 缠论叠加层验证 —— CDP 驱动（零依赖，Node 22 内置 fetch + WebSocket）
 *
 * 用法（Edge 已在 9222 就绪时）:
 *   node run-verify.mjs
 * 或自带浏览器（同一次 Bash 调用内起 Edge + 跑本脚本，见下方 run-with-edge.sh）
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const PAGE = 'file:///' + path.join(HERE, 'kline-verify.html').replace(/\\/g, '/')
const SHOT = path.join(HERE, 'verify.png')

const sleep = ms => new Promise(r => setTimeout(r, ms))

// 直连 page 级 WS（用 /json/list 拿到的 webSocketDebuggerUrl），省掉 sessionId 那套坑
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
  // sessionId 必须放顶层
  ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }))
})

// 无 sessionId: 直连 page target，方法直接可用
const call = (m, p = {}) => send(m, p)

await call('Page.enable')
await call('Runtime.enable')
await call('Emulation.setDeviceMetricsOverride', { width: 1400, height: 900, deviceScaleFactor: 1, mobile: false })
await call('Page.navigate', { url: PAGE })
await sleep(1200)

let done = false
for (let i = 0; i < 40; i++) {
  const r = await call('Runtime.evaluate', { expression: '!!window.__DONE__', returnByValue: true })
  if (r.result && r.result.result && r.result.result.value === true) { done = true; break }
  await sleep(500)
}
const res = await call('Runtime.evaluate', { expression: 'JSON.stringify(window.__RESULT__)', returnByValue: true })
const R = JSON.parse((res.result && res.result.result && res.result.result.value) || '{}')

console.log('===== KLineChart ' + R.version + ' 验证结果 =====')
console.log('done:', done)
console.log('数据: bars=' + R.data?.bars + ' px=[' + R.data?.pxMin + ', ' + R.data?.pxMax + ']')
console.log('图层: ' + JSON.stringify(R.data?.counts))
for (const [k, v] of Object.entries(R.checks || {})) {
  console.log((v.pass ? '  [PASS] ' : '  [FAIL] ') + k.padEnd(30) + JSON.stringify(v))
}
if (R.errs?.length) console.log('  ERRORS: ' + JSON.stringify(R.errs))
console.log('总判定: ' + (R.pass ? 'GO —— 可以开工 P1' : 'NO-GO —— 需走兜底'))

await sleep(300)
const shot = await call('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true })
if (shot.result && shot.result.data) {
  fs.writeFileSync(SHOT, Buffer.from(shot.result.data, 'base64'))
  console.log('截图: ' + SHOT)
}

ws.close()
await sleep(200)
process.exit(0)
