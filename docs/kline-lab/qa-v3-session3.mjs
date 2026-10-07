/**
 * v3 会话 QA 第三轮：视口修复版 + 信号 500 回归验证 + 完整页面巡检。
 */
import { openPage, Suite, sleep } from './qa-harness.mjs'

const BASE = 'http://127.0.0.1:3011'
const OUT = new URL('.', import.meta.url).pathname.replace(/^\/([A-Za-z]):/, '$1:')

const s1 = new Suite('K线加载与指标面板')
const s2 = new Suite('信号按钮')
const s3 = new Suite('页面切换健康度')

const page = await openPage()

async function gotoResize(url, waitMs = 15000) {
  const ok = await page.goto(url, waitMs)
  await page.resize(1600, 900) // 视口 override 可能被导航重置, 补一次
  return ok
}
async function shot(file) {
  const r = await page.send('Page.captureScreenshot', { format: 'png' })
  if (r.result?.data) {
    const { writeFileSync } = await import('node:fs')
    writeFileSync(file, Buffer.from(r.result.data, 'base64'))
  }
}
async function waitText(min = 120, timeoutMs = 10000) {
  const t0 = Date.now()
  while (Date.now() - t0 < timeoutMs) {
    const n = await page.eval('document.body.innerText.length').catch(() => 0)
    if (n > min) return { ok: true, ms: Date.now() - t0, textLen: n }
    await sleep(300)
  }
  return { ok: false, ms: -1, textLen: await page.eval('document.body.innerText.length').catch(() => -1) }
}
const clickByText = (text) => page.eval(`(() => {
  const b = [...document.querySelectorAll('button')].find(x => (x.innerText||'').trim() === '${text}')
  if (!b) return false; b.click(); return true
})()`)
const btnInfo = (text) => page.eval(`(() => {
  const b = [...document.querySelectorAll('button')].find(x => (x.innerText||'').trim() === '${text}')
  return b ? { title: b.title, disabled: b.disabled, cls: (b.className||'').slice(-60) } : null
})()`)
async function inkRatioBig() {
  return page.eval(`(() => {
    const cv = [...document.querySelectorAll('canvas')].filter(c => c.height > 150)
    if (!cv.length) return -1
    let ink = 0, total = 0
    for (const c of cv) {
      const ctx = c.getContext('2d'); if (!ctx) continue
      let d; try { d = ctx.getImageData(0, 0, c.width, c.height).data } catch { return -2 }
      for (let i = 3; i < d.length; i += 4 * 53) { total++; if (d[i] > 8) ink++ }
    }
    return total ? +(ink / total).toFixed(4) : -3
  })()`)
}
async function waitInk(timeoutMs = 20000) {
  const t0 = Date.now()
  while (Date.now() - t0 < timeoutMs) {
    const ink = await inkRatioBig().catch(() => -9)
    if (ink >= 0.02) return Date.now() - t0
    await sleep(200)
  }
  return -1
}

// ══════════ S1: 加载速度 + 布局 ══════════
console.log('\n━━ S1 K线加载与布局 ━━')
let t0 = Date.now()
const ok = await gotoResize(`${BASE}/stock/600522.SH`)
const inkMs = await waitInk()
s1.ok('首次加载 K 线渲染', ok && inkMs > 0, `navigate→ink ${inkMs}ms`)
const diag = await page.eval(`(() => {
  const r = el => { if (!el) return null; const b = el.getBoundingClientRect(); return { w: Math.round(b.width), h: Math.round(b.height) } }
  return {
    inner: [window.innerWidth, window.innerHeight],
    body: r(document.body),
    bigCanvas: [...document.querySelectorAll('canvas')].filter(c => c.height > 150).length,
    allCanvas: [...document.querySelectorAll('canvas')].map(c => c.height).filter(h => h > 0).length,
  }
})()`)
console.log('  [DIAG] ' + JSON.stringify(diag))
s1.ok('视口 1600x900', diag.inner[0] === 1600 && diag.inner[1] === 900, diag.inner.join('x'))
s1.ok('body 满高', diag.body.h > 500, `h=${diag.body.h}`)
s1.ok('主图 canvas 有尺寸', diag.bigCanvas > 0, `${diag.bigCanvas} 个大 canvas / ${diag.allCanvas} 个非零`)

// reload 二次计时
t0 = Date.now()
await page.reload()
await page.resize(1600, 900)
const inkMs2 = await waitInk()
s1.ok('reload 后重渲染', inkMs2 > 0, `${inkMs2}ms`)

// 切周期计时（周 → 日）
t0 = Date.now()
await clickByText('周')
const inkW = await waitInk(15000)
t0 = Date.now()
await clickByText('日')
const inkD = await waitInk(15000)
s1.ok('切周期(周/日)流畅', inkW > 0 && inkD > 0, `周 ${inkW}ms / 日 ${inkD}ms`)

// 工具条按钮
for (const label of ['定量结构', '信号', '缠论', '筹码', '指标 2']) {
  s1.ok(`按钮「${label}」存在`, !!(await btnInfo(label)))
}

// kline 接口耗时
const kts = await page.eval(`(() => performance.getEntriesByType('resource')
  .filter(r => r.name.includes('/api/kline')).map(r => Math.round(r.duration)).slice(-5))()`)
s1.note('kline 接口耗时(ms)', JSON.stringify(kts))

// 指标面板内容
await clickByText('指标 2')
await sleep(800)
const panelText = await page.eval('document.body.innerText')
s1.ok('指标面板含「MACD」(库内置)', panelText.includes('MACD'))
s1.note('「资金动能」在指标面板', panelText.includes('资金动能') ? '有' : '无（KLinePro 未注册自定义指标）')
s1.note('「蛟龙出海」在指标面板', panelText.includes('蛟龙出海') ? '有' : '无（KLinePro 未注册自定义指标）')
s1.note('「MACD定量结构」在指标面板', panelText.includes('MACD定量结构') ? '有' : '无（KLinePro 未注册自定义指标）')
await clickByText('指标 2')
await sleep(400)
await shot(OUT + 'qa3c-terminal.png')

// ══════════ S2: 信号按钮（回归：曾 500） ══════════
console.log('\n━━ S2 信号按钮（500 回归） ━━')
page.reset()
const digest0 = await page.canvasDigest()
const clicked = await clickByText('信号')
s2.ok('点击「信号」', clicked)
await sleep(3500)
const badAfterSig = page.netBad.filter(r => r.url.includes('/api/kline'))
s2.ok('开启信号后无 5xx 请求（回归项）', badAfterSig.length === 0,
  badAfterSig.map(r => `${r.status} ${r.url.slice(80, 140)}`).join('; ') || '干净')
const sigCols = await page.eval(`(async () => {
  const e = performance.getEntriesByType('resource').filter(r => r.name.includes('/api/kline')).slice(-1)[0]
  if (!e) return null
  const j = await (await fetch(e.name)).json()
  const rows = j.rows || []
  if (!rows.length) return { cols: [], hits: 0 }
  const cols = Object.keys(rows[0]).filter(k => k.startsWith('signal_') || k === 'consecutive_limit_ups')
  const hits = rows.filter(r => Object.entries(r).some(([k, v]) => k.startsWith('signal_') && v === true)).length
  return { cols: cols.slice(0, 8), colN: cols.length, hits }
})()`)
console.log('  [SIG-COLS] ' + JSON.stringify(sigCols))
s2.ok('响应含信号列', !!sigCols && sigCols.colN > 0, `${sigCols?.colN} 列 / 命中行 ${sigCols?.hits}`)
s2.note('窗口内信号命中行数', String(sigCols?.hits))
const digest1 = await page.canvasDigest()
s2.ok('画面发生变化', digest0 !== digest1, `${digest0} → ${digest1}`)
const markerPixels = await page.eval(`(() => {
  let red = 0, green = 0, blue = 0
  for (const c of document.querySelectorAll('canvas')) {
    if (!c.width || c.height < 150) continue
    const ctx = c.getContext('2d'); if (!ctx) continue
    let d; try { d = ctx.getImageData(0, 0, c.width, c.height).data } catch { continue }
    for (let i = 0; i < d.length; i += 4) {
      const r = d[i], g = d[i+1], b = d[i+2], a = d[i+3]
      if (a < 40) continue
      if (r > 140 && r > g + 50 && r > b + 50) red++
      else if (g > 130 && g > r + 40 && g > b + 30) green++
      else if (b > 150 && b > r + 40 && b > g + 20) blue++
    }
  }
  return { red, green, blue }
})()`)
s2.note('标记像素 red/green/blue', JSON.stringify(markerPixels))
await shot(OUT + 'qa3c-terminal-sig.png')

// 周线档行为
await clickByText('周')
await sleep(2500)
const sigWeek = await btnInfo('信号')
s2.note('周线档信号按钮', JSON.stringify(sigWeek))
await clickByText('日')
await sleep(2000)

// ══════════ S3: 页面巡检（预热+验证两遍） ══════════
console.log('\n━━ S3 页面巡检 ━━')
const routes = [
  ['watchlist', '自选', true],
  ['screener', '选股', false],
  ['chan-scan', '缠论扫描', false],
  ['backtest', '回测', true],
  ['signal-lab', '信号实验室', true],
  ['signal-catalog', '信号函数库', true],
  ['pulse', '盘中脉搏', false],
  ['monitor', '监控', false],
  ['limit-ladder', '涨停梯队', false],
  ['indices', '指数', false],
  ['regime', '市场状态', false],
  ['abnormal', '异动', false],
  ['review', '复盘', true],
  ['mining', '挖掘', false],
  ['financials', '财务', false],
  ['data', '数据', false],
  ['news', '新闻', false],
  ['settings', '设置', false],
]
console.log('  -- 预热遍 --')
for (const [path] of routes) { await gotoResize(`${BASE}/${path}`, 15000).catch(() => {}); await sleep(200) }
console.log('  -- 验证遍 --')
for (const [path, name, doShot] of routes) {
  page.reset()
  const okG = await gotoResize(`${BASE}/${path}`)
  const w = await waitText(120, 10000)
  s3.ok(`${name}(${path}) 渲染`, okG && w.ok, `text=${w.textLen} ${w.ms}ms`)
  const errs = [...page.exceptions, ...page.consoleErrors].filter(e => !/favicon|ResizeObserver/i.test(e))
  if (errs.length) s3.note(`${name} console/异常`, errs.slice(0, 2).join(' ; ').slice(0, 200))
  const bad = page.netBad.filter(r => !r.url.includes('favicon'))
  if (bad.length) s3.note(`${name} HTTP>=400`, bad.slice(0, 3).map(r => `${r.status} ${(r.url.split('/api/')[1] || r.url).slice(0, 70)}`).join(' ; ').slice(0, 200))
  if (doShot) await shot(OUT + `qa3c-page-${path}.png`)
}

console.log('\n━━━ 汇总 ━━━')
let allOk = true
allOk = s1.summary() && allOk
allOk = s2.summary() && allOk
allOk = s3.summary() && allOk
console.log(`\nFINAL: ${allOk ? 'ALL PASS' : 'HAS FAILURES'}`)
process.exit(allOk ? 0 : 1)
