/**
 * S2 指标管理器真机验收 —— CDP 驱动，零依赖。
 *
 * 前置：后端 3018 + 前端 3011 + Edge(--remote-debugging-port=9222) 都在跑。
 * 用法：node run-indicator-check.mjs [symbol]
 *
 * 检查项：
 *   I1  KLinePro 内核起来，默认指标 MA(主图) + VOL(副图) 已从库内回读到真实参数
 *   I2  主副图分窗(canvas 数量 >= 2，klinecharts 每个窗格一层 canvas)
 *   I3  「指标」按钮打开管理面板
 *   I4  添加副图指标 MACD -> 持久化多一条 + 窗格增加
 *   I5  改 MA 第一个参数 -> 持久化跟着变，且不重建窗格(窗格数不变)
 *   I6  删除 MACD -> 持久化回落 + 窗格减少
 *   I7  全程无 JS 异常 / console error
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
const snap = async name => {
  const r = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true })
  return r.result?.data ? shot(name, Buffer.from(r.result.data, 'base64')) : null
}
const clickText = async (text, exact = false) => evaluate(`(() => {
  const b = [...document.querySelectorAll('button')].find(x => ${exact ? `x.textContent.trim() === '${text}'` : `x.textContent.trim().startsWith('${text}')`})
  if (!b) return false
  b.click()
  return true
})()`)

const results = {}
const ok = (n, cond, detail) => { results[n] = { pass: !!cond, detail }; console.log(`  ${cond ? '[PASS]' : '[FAIL]'} ${n}  ${detail ?? ''}`) }

await send('Page.enable')
await send('Runtime.enable')
await send('Log.enable')
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false })

// 从干净状态开始：启用 KLinePro，清掉指标持久化(走默认 MA+VOL)
await send('Page.navigate', { url: URL0 })
await sleep(2500)
await evaluate(`
  localStorage.setItem('tickflow.useKLinePro','true');
  localStorage.removeItem('tickflow.kline.indicators.v1');
  localStorage.removeItem('tickflow.kline.paneHeights.v1');
  'ok'
`)
await send('Page.reload')
await sleep(3500)

let canvases = 0
for (let i = 0; i < 24; i++) {
  canvases = await evaluate(`document.querySelectorAll('canvas').length`)
  if (canvases > 0) break
  await sleep(500)
}
await sleep(1500)

const readStore = () => evaluate(`(() => {
  try { return JSON.parse(localStorage.getItem('tickflow.kline.indicators.v1') || '[]') } catch { return [] }
})()`)

const s0 = await readStore()
const c0 = await evaluate(`document.querySelectorAll('canvas').length`)
console.log('默认 store:', JSON.stringify(s0), 'canvas=', c0)
ok('I1_defaultIndicators', s0.length === 2 && s0.some(x => x.name === 'MA' && x.group === 'main') && s0.some(x => x.name === 'VOL' && x.group === 'sub'), JSON.stringify(s0.map(x => `${x.name}:${x.group}:${x.params.length}`)))
ok('I1b_paramsReadBack', (s0.find(x => x.name === 'MA')?.params?.length ?? 0) > 0, `MA params=${JSON.stringify(s0.find(x => x.name === 'MA')?.params)}`)
ok('I2_multiPane', c0 >= 2, `canvas=${c0}`)
await snap('indicator-1-default.png')

// I3 打开面板
const opened = await clickText('指标')
await sleep(600)
let panel = await evaluate(`(document.body.innerText||'').includes('添加副图')`)
if (!panel) { await sleep(1200); panel = await evaluate(`(document.body.innerText||'').includes('添加副图')`) }
ok('I3_openManager', opened && panel, `clicked=${opened} panel=${panel}`)
await snap('indicator-2-manager.png')

// I4 添加 MACD
const added = await clickText('指数平滑异同')
await sleep(1500)
const s1 = await readStore()
const c1 = await evaluate(`document.querySelectorAll('canvas').length`)
ok('I4_addMacd', s1.length === 3 && s1.some(x => x.name === 'MACD'), `store=${JSON.stringify(s1.map(x => x.name))}`)
ok('I4b_paneAdded', c1 > c0, `canvas ${c0} -> ${c1}`)
await snap('indicator-3-macd.png')

// I5 改 MA 第一个参数
const before = JSON.stringify((await readStore()).find(x => x.name === 'MA')?.params)
const edited = await evaluate(`(() => {
  // 已添加区第一个数字输入框就是 MA 的第一个参数(MA 是默认清单里的第一项)
  const input = document.querySelector('input[type=number]')
  if (!input) return 'no-input'
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
  setter.call(input, '10')
  input.dispatchEvent(new Event('input', { bubbles: true }))
  return 'ok'
})()`)
await sleep(1200)
const after = JSON.stringify((await readStore()).find(x => x.name === 'MA')?.params)
const c2 = await evaluate(`document.querySelectorAll('canvas').length`)
ok('I5_editParam', edited === 'ok' && before !== after, `${edited} params ${before} -> ${after}`)
ok('I5b_noPaneChurn', c2 === c1, `canvas ${c1} -> ${c2}`)
await snap('indicator-4-param.png')

// I6 删除 MACD
const removed = await evaluate(`(() => {
  const btns = [...document.querySelectorAll('button')].filter(b => b.title === '删除')
  const rows = btns.filter(b => (b.closest('div')?.textContent || '').includes('MACD'))
  if (rows.length === 0) return false
  rows[rows.length - 1].click()
  return true
})()`)
await sleep(1500)
const s2 = await readStore()
const c3 = await evaluate(`document.querySelectorAll('canvas').length`)
ok('I6_removeMacd', removed && s2.length === 2 && !s2.some(x => x.name === 'MACD'), `store=${JSON.stringify(s2.map(x => x.name))}`)
ok('I6b_paneRemoved', c3 < c1, `canvas ${c1} -> ${c3}`)
await snap('indicator-5-removed.png')

// I7 刷新后仍保留（持久化）
await send('Page.reload')
await sleep(4000)
const s3 = await readStore()
ok('I7_persistAfterReload', s3.length === 2 && (s3.find(x => x.name === 'MA')?.params?.[0] === 10), JSON.stringify(s3))

ok('I8_noJsExceptions', errors.length === 0, JSON.stringify(errors.slice(0, 3)))
ok('I8b_noConsoleErrors', consoleErrors.length === 0, JSON.stringify(consoleErrors.slice(0, 3)))

const pass = Object.values(results).every(r => r.pass)
console.log('\n总判定: ' + (pass ? 'PASS —— S2 指标管理真机可用' : 'FAIL —— 见上'))
if (errors.length) console.log('exceptions:\n  ' + errors.join('\n  '))
if (consoleErrors.length) console.log('console errors:\n  ' + consoleErrors.join('\n  '))

ws.close()
await sleep(200)
process.exit(pass ? 0 : 1)
