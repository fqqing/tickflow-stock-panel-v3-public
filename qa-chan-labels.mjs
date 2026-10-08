/**
 * QA: 缠论买卖点「几买/几卖」文字标签渲染复现。
 * 用法: node qa-chan-labels.mjs
 * 依赖: Edge headless (--remote-debugging-port) + Node 24 内置 WebSocket/fetch
 */
import { spawn } from 'node:child_process'
import { writeFileSync, mkdirSync } from 'node:fs'

const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
const PORT = 9333
const BASE = 'http://127.0.0.1:3018'
// 从 auth.json 取第一个 session（与面板登录态同源）
const { readFileSync } = await import('node:fs')
const auth = JSON.parse(readFileSync('data/user_data/auth.json', 'utf-8'))
const SID = Object.keys(auth.sessions)[0]

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function cdpSend(ws, id, method, params = {}) {
  return new Promise((resolve, reject) => {
    const onMsg = (ev) => {
      const m = JSON.parse(ev.data)
      if (m.id === id) {
        ws.removeEventListener('message', onMsg)
        if (m.error) reject(new Error(method + ': ' + JSON.stringify(m.error)))
        else resolve(m.result)
      }
    }
    ws.addEventListener('message', onMsg)
    ws.send(JSON.stringify({ id, method, params }))
  })
}

async function evalJS(ws, expr, id) {
  const r = await cdpSend(ws, id, 'Runtime.evaluate', {
    expression: expr,
    returnByValue: true,
    awaitPromise: true,
  })
  if (r.exceptionDetails) throw new Error('page eval: ' + JSON.stringify(r.exceptionDetails).slice(0, 500))
  return r.result.value
}

// ── 1. 启动 Edge headless ──
mkdirSync('data/user_data/_qa_tmp/edge-profile', { recursive: true })
const edge = spawn(EDGE, [
  '--headless=new',
  `--remote-debugging-port=${PORT}`,
  '--user-data-dir=D:/project/GP/tickflow-stock-panel-v3/data/user_data/_qa_tmp/edge-profile',
  '--window-size=1680,940',
  '--no-first-run',
  'about:blank',
], { stdio: 'ignore' })

try {
  // 等 debug 端口
  let ver = null
  for (let i = 0; i < 30; i++) {
    await sleep(500)
    try {
      ver = await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json()
      break
    } catch { /* retry */ }
  }
  if (!ver) throw new Error('Edge debug port 未就绪')

  // 新开 tab
  const tab = await (await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: 'PUT' })).json()
  const ws = new WebSocket(tab.webSocketDebuggerUrl)
  await new Promise((r, j) => { ws.onopen = r; ws.onerror = j })
  let seq = 1
  await cdpSend(ws, seq++, 'Page.enable')
  await cdpSend(ws, seq++, 'Runtime.enable')

  // ── 2. 先到同域登录页注入 session cookie ──
  await cdpSend(ws, seq++, 'Page.navigate', { url: `${BASE}/login` })
  await sleep(2500)
  await evalJS(ws, `document.cookie = 'tf_session=${SID}; path=/'`, seq++)
  // ── 3. 进个股终端 ──
  await cdpSend(ws, seq++, 'Page.navigate', { url: `${BASE}/stock/301119.SZ` })
  await sleep(6000)

  // 等 K 线 canvas 出现
  const hasCanvas = await evalJS(ws, `document.querySelectorAll('canvas').length`, seq++)
  console.log('canvas 数量:', hasCanvas)

  // ── 4. 点「缠论」开关 ──
  const clicked = await evalJS(ws, `(() => {
    const btn = [...document.querySelectorAll('button')].find(b => (b.title||'').includes('缠论'))
    if (!btn) return 'NOT_FOUND'
    const active = btn.className.includes('active') || btn.getAttribute('data-active')
    btn.click()
    return 'CLICKED title=' + btn.title
  })()`, seq++)
  console.log('缠论按钮:', clicked)
  await sleep(7000) // 等 chan API + overlay 渲染

  // ── 5. 采样: 页面内 fetch chan API 对照 + canvas 像素扫描 ──
  const chan = await evalJS(ws, `fetch('/api/chan/analysis?symbol=301119.SZ&lookback=400&adjust=qfq', {headers:{Cookie:'tf_session=${SID}'}}).then(r=>r.json()).then(d => ({
    bars: d.bars, n_signals: (d.signals||[]).length,
    signals: (d.signals||[]).map(s => ({date: s.date, kind: s.kind, label: s.label}))
  }))`, seq++)
  console.log('前端视角 chan API:', JSON.stringify(chan))

  const scan = await evalJS(ws, `(() => {
    const out = []
    for (const cv of document.querySelectorAll('canvas')) {
      const r = cv.getBoundingClientRect()
      const ctx = cv.getContext('2d')
      if (!ctx) continue
      const img = ctx.getImageData(0, 0, cv.width, cv.height).data
      let red = 0, blue = 0, orange = 0
      for (let i = 0; i < img.length; i += 4) {
        const [R, G, B] = [img[i], img[i+1], img[i+2]]
        if (R > 200 && G < 110 && B < 100) red++               // #F04438 红字/红箭头
        else if (B > 200 && R < 120 && G > 100 && G < 190) blue++ // #3B82F6 蓝
        else if (R > 200 && G > 120 && G < 190 && B < 80) orange++
      }
      out.push({ w: cv.width, h: cv.height, css: Math.round(r.width) + 'x' + Math.round(r.height) + '@' + Math.round(r.top), red, blue, orange })
    }
    return out
  })()`, seq++)
  console.log('canvas 像素扫描:', JSON.stringify(scan, null, 1))

  // ── 6. 截图 ──
  const shot = await cdpSend(ws, seq++, 'Page.captureScreenshot', { format: 'png' })
  mkdirSync('data/user_data/_qa_tmp', { recursive: true })
  writeFileSync('data/user_data/_qa_tmp/chan-labels-full.png', Buffer.from(shot.data, 'base64'))
  console.log('截图: data/user_data/_qa_tmp/chan-labels-full.png')

  // ── 7. 缠论按钮位置信息（蓝块来源排查: 按钮激活态颜色） ──
  const chips = await evalJS(ws, `[...document.querySelectorAll('button')].filter(b=>b.title).map(b=>({t: b.title.slice(0,18), cls: b.className.slice(0,80)})).slice(0,20)`, seq++)
  console.log('工具栏按钮:', JSON.stringify(chips, null, 1))

  ws.close()
} finally {
  edge.kill()
}
