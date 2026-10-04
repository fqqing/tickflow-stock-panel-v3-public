/**
 * S3 筹码分布真机验收 —— CDP 驱动，零依赖。
 *
 * 前置：后端 3018 + 前端 3011 + Edge(--remote-debugging-port=9222) 都在跑。
 * 用法：node run-chips-check.mjs [symbol]
 *
 * 检查项：
 *   C1  KLinePro 起来(canvas 存在)
 *   C2  「筹码」按钮存在且可点
 *   C3  点击后按钮进入激活态 + 出现「成本 / 获利」统计文本(说明 chips 数据取到)
 *   C4  统计数值与 /api/stock-analysis/chips 返回一致
 *   C5  再点一次 -> 统计文本消失(overlay 被移除)
 *   C6  全程无 JS 异常 / console error
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const SYMBOL = process.argv[2] || '600519.SH'
const URL0 = `http://127.0.0.1:3011/stock/${SYMBOL}`
const sleep = ms => new Promise(r => setTimeout(r, ms))
const shot = (name, buf) => {
  const f = path.join(HERE, name)
  fs.writeFileSync(f, buf)
  return f
}

const list = await (await fetch('http://127.0.0.1:9222/json/list')).json()
const page = list.find(t => t.type === 'page' && t.webSocketDebuggerUrl)
if (!page) throw new Error('no page target (Edge 9222 起了吗?)')
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
    consoleErrors.push(String(msg.params.args?.[0]?.value ?? '').slice(0, 200))
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
const results = {}
const ok = (name, pass, detail = '') => {
  results[name] = { pass, detail }
  console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`)
}
const snap = async name => {
  const r = await send('Page.captureScreenshot', { format: 'png' })
  const buf = Buffer.from(r.result.data, 'base64')
  return shot(name, buf)
}

await send('Page.enable')
await send('Runtime.enable')
await send('Page.navigate', { url: URL0 })
await sleep(6000)
// KLinePro 是灰度内核(默认 ECharts), 先把开关打开再刷新
await evaluate(`localStorage.setItem('tickflow.useKLinePro', 'true')`)
await send('Page.reload')
await sleep(10000)

const canvases = await evaluate(`document.querySelectorAll('canvas').length`)
ok('C1_chartReady', Number(canvases) >= 1, `canvas=${canvases}`)

// 后端筹码口径(作为前端展示的基准)
const apiData = await evaluate(`(async () => {
  const r = await fetch('/api/stock-analysis/chips?symbol=${SYMBOL}&days=250&bins=60')
  const j = await r.json()
  return { ok: j.ok, avg: j.avg_cost, profit: j.profit_ratio, bins: j.bins?.length ?? 0 }
})()`)
ok('C1b_apiOk', !!apiData?.ok && apiData.bins > 0, JSON.stringify(apiData))

// 点「筹码」按钮
const clicked = await evaluate(`(() => {
  const btn = [...document.querySelectorAll('button')].find(b => b.textContent.trim() === '筹码')
  if (!btn) return 'no-button'
  if (btn.disabled) return 'disabled'
  btn.click()
  return 'clicked'
})()`)
ok('C2_buttonClickable', clicked === 'clicked', clicked)
await sleep(6000)

const stat = await evaluate(`(() => {
  const el = [...document.querySelectorAll('span')].find(s => s.textContent.includes('成本') && s.textContent.includes('获利'))
  if (!el) return null
  const nums = (el.textContent.match(/[\\d.]+/g) || [])
  return { text: el.textContent.trim(), nums }
})()`)
ok('C3_statShown', !!stat, stat ? stat.text : 'no stat span')
if (stat && apiData) {
  const avgShown = stat.nums[0]
  const profitShown = stat.nums[1]
  const okAvg = Math.abs(Number(avgShown) - Number(apiData.avg)) < 0.02
  const okProfit = Math.abs(Number(profitShown) - Number((apiData.profit * 100).toFixed(1))) < 0.2
  ok('C4_valuesMatchApi', okAvg && okProfit,
    `shown=${avgShown}/${profitShown}% api=${Number(apiData.avg).toFixed(2)}/${(apiData.profit * 100).toFixed(1)}%`)
}
await snap('chips-1-on.png')

// 关掉
await evaluate(`(() => {
  const btn = [...document.querySelectorAll('button')].find(b => b.textContent.trim() === '筹码')
  if (btn) btn.click()
  return true
})()`)
await sleep(2500)
const statAfter = await evaluate(`(() => {
  const el = [...document.querySelectorAll('span')].find(s => s.textContent.includes('成本') && s.textContent.includes('获利'))
  return !!el
})()`)
ok('C5_toggleOff', statAfter === false, `statAfter=${statAfter}`)
await snap('chips-2-off.png')

ok('C6_noJsExceptions', errors.length === 0, JSON.stringify(errors.slice(0, 3)))
ok('C6b_noConsoleErrors', consoleErrors.length === 0, JSON.stringify(consoleErrors.slice(0, 3)))

const pass = Object.values(results).every(r => r.pass)
console.log('\n总判定: ' + (pass ? 'PASS —— S3 筹码分布真机可用' : 'FAIL —— 见上'))
if (errors.length) console.log('exceptions:\n  ' + errors.join('\n  '))
if (consoleErrors.length) console.log('console errors:\n  ' + consoleErrors.slice(0, 5).join('\n  '))

ws.close()
await sleep(200)
process.exit(pass ? 0 : 1)
