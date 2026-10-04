// 验证: 蛟龙出海/主图定量结构 按钮已从底部副图栏移到顶部工具条
// 用法: 先起 Edge headless (9222), 再 node run-overlay-check.mjs
import { writeFileSync } from 'node:fs'

const BASE = process.env.BASE || 'http://127.0.0.1:3011'
const SYMBOL = process.env.SYMBOL || '300951.SZ'
const CDP = 'http://127.0.0.1:9222'

const sleep = (ms) => new Promise(r => setTimeout(r, ms))

async function getTarget() {
  for (let i = 0; i < 10; i++) {
    try {
      const list = await (await fetch(`${CDP}/json`)).json()
      const page = list.find(t => t.type === 'page')
      if (page) return page
    } catch { /* retry */ }
    await sleep(1000)
  }
  throw new Error('no CDP page target')
}

const target = await getTarget()
const ws = new WebSocket(target.webSocketDebuggerUrl)
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej })

let msgId = 0
const pending = new Map()
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data)
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id) }
}
function call(method, params = {}) {
  const id = ++msgId
  return new Promise((resolve, reject) => {
    pending.set(id, (m) => m.error ? reject(new Error(`${method}: ${m.error.message}`)) : resolve(m.result))
    ws.send(JSON.stringify({ id, method, params, sessionId: undefined }))
  })
}
async function evalJs(expression) {
  const r = await call('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
  if (r.exceptionDetails) throw new Error('页面 JS 异常: ' + JSON.stringify(r.exceptionDetails).slice(0, 500))
  return r.result.value
}

await call('Page.enable')
await call('Runtime.enable')
await call('Emulation.setDeviceMetricsOverride', { width: 1600, height: 900, deviceScaleFactor: 1, mobile: false })

await call('Page.navigate', { url: `${BASE}/stock/${SYMBOL}` })
// 等 K 线数据加载(底部按钮出现)
for (let i = 0; i < 40; i++) {
  const ok = await evalJs(`[...document.querySelectorAll('button')].some(b => b.textContent.trim() === '蛟龙出海')`)
  if (ok) break
  await sleep(1000)
}
await sleep(3000)

const report = await evalJs(`(() => {
  const btns = [...document.querySelectorAll('button')]
  // 底部副图指标栏: 含「成交额」按钮的那个容器
  const volBtn = btns.find(b => b.textContent.trim() === '成交额')
  const bottomBar = volBtn ? volBtn.parentElement : null
  const bottomLabels = bottomBar ? [...bottomBar.querySelectorAll('button')].map(b => b.textContent.trim()) : []
  // 顶部工具条: 含「前复权」按钮的容器行
  const adjBtn = btns.find(b => b.textContent.trim() === '前复权')
  const topBar = adjBtn ? adjBtn.closest('div').parentElement : null
  const topLabels = topBar ? [...topBar.querySelectorAll('button')].map(b => b.textContent.trim()) : []
  // 蛟龙出海/主图定量结构 的位置
  const pos = {}
  for (const label of ['蛟龙出海', '主图定量结构']) {
    const b = btns.find(x => x.textContent.trim() === label)
    pos[label] = {
      found: !!b,
      inBottomBar: bottomBar ? bottomBar.contains(b) : false,
      disabled: b ? b.disabled : null,
    }
  }
  return { bottomLabels, topLabels, pos }
})()`)
console.log('底部栏按钮:', JSON.stringify(report.bottomLabels))
console.log('顶部栏按钮:', JSON.stringify(report.topLabels))
console.log('位置判定:', JSON.stringify(report.pos, null, 2))

// 截图: 整页 + 顶部工具条区域
const shot = await call('Page.captureScreenshot', { format: 'png' })
writeFileSync(new URL('./overlay-top-check.png', import.meta.url), Buffer.from(shot.data, 'base64'))

// 切到 30 分钟档验证禁用态
const minBtn = await evalJs(`(() => {
  const b = [...document.querySelectorAll('button')].find(x => x.textContent.trim() === '30分')
  if (b) { b.click(); return true } return false
})()`)
if (minBtn) {
  // 等 30 分数据加载完(工具条重新挂载)
  for (let i = 0; i < 30; i++) {
    const back = await evalJs(`[...document.querySelectorAll('button')].some(x => x.textContent.trim() === '蛟龙出海')`)
    if (back) break
    await sleep(1000)
  }
  await sleep(1500)
  const disabledState = await evalJs(`(() => {
    const out = {}
    for (const label of ['蛟龙出海', '主图定量结构']) {
      const b = [...document.querySelectorAll('button')].find(x => x.textContent.trim() === label)
      out[label] = b ? b.disabled : 'not-found'
    }
    return out
  })()`)
  console.log('30分周期下禁用态:', JSON.stringify(disabledState))
  const shot2 = await call('Page.captureScreenshot', { format: 'png' })
  writeFileSync(new URL('./overlay-minute-disabled.png', import.meta.url), Buffer.from(shot2.data, 'base64'))
}

const ok = report.pos['蛟龙出海'].found && report.pos['主图定量结构'].found
  && !report.pos['蛟龙出海'].inBottomBar && !report.pos['主图定量结构'].inBottomBar
console.log(ok ? 'PASS: 两个按钮已不在底部副图栏' : 'FAIL: 按钮位置不符合预期')
ws.close()
process.exit(ok ? 0 : 1)
