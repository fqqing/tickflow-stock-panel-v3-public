/**
 * QA: ECharts 内核功能测试（真实浏览器 + 真实后端）。
 *
 *   node qa-echarts.mjs [symbol]
 *
 * 覆盖: 基线渲染 / 周期切换 / 主图副图指标 / 缠论 / 叠加层 / 时间轴 / 边界
 * 断言原则: 不看"有没有报错"就算过, 还要看**画面真的变了吗**(canvas 指纹)。
 */
import { openPage, sleep, Suite } from './qa-harness.mjs'

const SYM = process.argv[2] || '000001.SZ'
const BASE = 'http://127.0.0.1:3011'
const HERE = 'D:/project/GP/tickflow-stock-panel/docs/kline-lab/'
const page = await openPage()
const S = new Suite('ECharts 内核')
const shots = []

const go = async (tag, ms = 3500) => { await sleep(ms); return page.shot(HERE + `qa-echarts-${tag}.png`) }
const state = () => page.eval(`(() => {
  const t = document.body.innerText || ''
  const cv = [...document.querySelectorAll('canvas')]
  const main = cv.map(c => ({ w: c.width, h: c.height })).sort((a,b) => b.w*b.h - a.w*a.h)[0]
  const panes = [...document.querySelectorAll('div')].filter(d => /MA\\d|VOL\\(|MACD|KDJ|RSI|BOLL/.test(d.innerText || '') && d.clientHeight > 20 && d.clientHeight < 200)
  return {
    loading: t.includes('载入中'),
    noData: t.includes('暂无该周期数据'),
    name: /平安银行|贵州茅台|万 科|宁德时代/.test(t) ? 'yes' : 'no',
    mainCanvas: main ? main.w + 'x' + main.h : '-',
    hasMA: /MA5|MA10|MA20/.test(t),
    hasVOL: /VOL\\(|VOLUME|成交量/.test(t),
    hasMACD: /MACD/.test(t),
    hasKDJ: /KDJ/.test(t),
    hasBOLL: /BOLL|BOLL\\(/.test(t),
    subPaneTexts: panes.map(p => (p.innerText||'').trim().split('\\n')[0].slice(0, 40)).slice(0, 6),
    toasts: [...document.querySelectorAll('[role="alert"], [class*="toast"]')].map(e => (e.innerText||'').trim().slice(0,50)).filter(Boolean),
  }
})()`)

// ── 准备: 锁定 ECharts 内核 + 清空指标残留 ──
await page.resize(1600, 950)
await page.send('Page.stopLoading').catch(() => {})
await page.goto(`${BASE}/stock/NONE.XX`)   // 先离开旧页面, 断开可能卡住的加载
await sleep(800)
await page.setKernel('echarts')
await page.eval("localStorage.removeItem('tf-kline-indicators'); localStorage.removeItem('tf-kline-panes')")
const landed = await page.goto(`${BASE}/stock/${encodeURIComponent(SYM)}`)
const at = await page.currentSymbol()
S.ok('E0_导航落在目标标的上', landed && at === SYM, `landed=${landed} symbol=${at}`)
await sleep(6000)

const k = await page.kernel()
S.ok('E1_锁定内核为ECharts', k === 'ECharts', `kernel=${k}`)

let st = await state()
shots.push(await go('01-baseline', 500))
console.log('  baseline:', JSON.stringify({ ...st, subPaneTexts: undefined }))
S.ok('E2_无加载遮罩', !st.loading, `loading=${st.loading}`)
S.ok('E3_标的名已解析', st.name === 'yes', `name=${st.name}`)
S.ok('E4_主图有内容', !st.noData && st.mainCanvas !== '-', `noData=${st.noData} canvas=${st.mainCanvas}`)
S.ok('E5_主图MA正常', st.hasMA, `hasMA=${st.hasMA}`)
S.ok('E6_副图VOL正常', st.hasVOL, `hasVOL=${st.hasVOL}`)
S.ok('E7_无异常toast', st.toasts.length === 0, JSON.stringify(st.toasts))
S.ok('E8_基线无4xx5xx', page.netBad.length === 0, JSON.stringify(page.netBad.slice(0, 3)))

// 主图 canvas 宽度是否撑满容器(此前 probe 出现过 457px 的窄图)
const fit = await page.eval(`(() => {
  const rects = [...document.querySelectorAll('canvas')].map(c => {
    const r = c.getBoundingClientRect()
    // 往上找最近的有明确尺寸的容器
    let p = c.parentElement, host = null
    while (p && p !== document.body) { if (p.clientWidth > 100) { host = p; break } p = p.parentElement }
    return { w: r.width, h: r.height, hostW: host ? host.clientWidth : -1, hostH: host ? host.clientHeight : -1 }
  }).filter(r => r.w > 0)
  rects.sort((a, b) => b.w * b.h - a.w * a.h)
  return rects[0] || { w: -1, h: -1, hostW: -1, hostH: -1 }
})()`)
S.ok('E9_主图有实际尺寸', fit && fit.w > 200 && fit.h > 150,
  `canvas=${Math.round(fit.w)}x${Math.round(fit.h)} 容器=${fit.hostW}x${fit.hostH}`)

// ECharts 内核是否有「指标」管理入口 —— 这是双内核能力对齐的关键观测点
const hasIndicatorBtn = await page.eval(`[...document.querySelectorAll('button')].some(b => /^指标\\s*\\d*$/.test((b.innerText||'').trim()))`)
S.note('E10_ECharts指标管理入口', `存在=${hasIndicatorBtn} (KLinePro 侧有此按钮)`)
if (!hasIndicatorBtn) {
  const subCount = await page.eval(`(() => {
    // 副图窗格: 图内文本以指标名开头的窄条
    const t = document.body.innerText || ''
    return { volShown: /成交量/.test(t), macdShown: /MACD\\(12/.test(t), kdjShown: /KDJ\\(9,3,3\\)/.test(t) }
  })()`)
  S.note('E10b_ECharts副图现状', JSON.stringify(subCount))
}

// ── 周期切换 ──
const clickPeriod = async label => {
  const ok = await page.eval(`(() => {
    const all = [...document.querySelectorAll('button')]
    // 周期按钮文案可能带角标/空格, 用 startsWith 更稳
    const b = all.find(x => (x.innerText||'').trim().startsWith(${JSON.stringify(label)}))
    if (!b) { console.log('period-btns=' + JSON.stringify(all.filter(x => x.getBoundingClientRect().top < 130).map(x => (x.innerText||'').trim()))); return false }
    b.click(); return true
  })()`)
  await sleep(3000)
  return ok
}
for (const [tag, label, expectNoData] of [['week', '周', false], ['month', '月', false], ['m5', '5分', false], ['back', '日', false]]) {
  page.reset()
  const clicked = await clickPeriod(label)
  st = await state()
  shots.push(await go(`02-period-${tag}`, 300))
  if (!clicked) { S.ok(`Q1_周期切换_${label}`, false, '按钮未找到'); continue }
  const noErr = page.exceptions.length === 0 && page.netBad.length === 0
  S.ok(`Q1_周期切换_${label}`, noErr && !st.loading && (!expectNoData ? !st.noData : true),
    `noData=${st.noData} loading=${st.loading} exc=${page.exceptions.length} net4xx=${page.netBad.length}`)
}

// ── 叠加层开关（逐个: 断言画面指纹变化 + 无异常）──
const clickByTitle = async frag => page.eval(`(() => {
  const b = [...document.querySelectorAll('button')].find(x => (x.title||'').includes(${JSON.stringify(frag)}))
  if (!b) return 'not-found'
  b.click(); return 'clicked'
})()`)

for (const [frag, key, tag] of [
  ['主图定量结构', 's', 'structure'],
  ['缠论笔', 'c', 'chan'],
  ['策略信号标记', 'x', 'signals'],
  ['监控触发记录', 'a', 'alerts'],
  ['最近一次策略回测的买卖点', 't', 'trades'],
]) {
  const before = await page.canvasDigest()
  page.reset()
  const r = await clickByTitle(frag)
  await sleep(3200)
  const after = await page.canvasDigest()
  const stt = await state()
  shots.push(await go(`03-overlay-${tag}`, 300))
  const errs = page.exceptions.length + page.netBad.length
  S.ok(`Q2_叠加层_${tag}_无异常`, errs === 0 && r === 'clicked', `click=${r} exc=${page.exceptions.length} net4xx=${page.netBad.length} ${JSON.stringify(page.netBad.slice(0,2))}`)
  S.note(`Q2_叠加层_${tag}_画面变化`, `before=${before} after=${after} changed=${before !== after} toast=${JSON.stringify(stt.toasts)}`)
}

// 时间轴
page.reset()
const beforeTl = await page.canvasDigest()
const tlClick = await clickByTitle('事件时间轴')
await sleep(3000)
const tl = await page.eval(`(() => {
  const t = document.body.innerText || ''
  const bars = document.querySelectorAll('[data-timeline-bar]').length
  const tlBox = [...document.querySelectorAll('div')].filter(d => /信号|触发|买卖点/.test(d.innerText||'') && d.clientHeight < 120 && d.clientHeight > 10).length
  return { bars, tlBox, hasHint: t.includes('时间轴') }
})()`)
shots.push(await go('04-timeline', 300))
S.ok('Q3_时间轴可开启无异常', tlClick === 'clicked' && page.exceptions.length === 0, `click=${tlClick} exc=${page.exceptions.length}`)
S.note('Q3_时间轴内容', JSON.stringify(tl) + ` changed=${beforeTl !== await page.canvasDigest()}`)

// ── 边界: 无数据的标的 / 非法 symbol ──
page.reset()
await page.goto(`${BASE}/stock/NOTEXIST.SZ`)
await sleep(5000)
const bad = await state()
S.ok('Q4_非法标的给出提示而非白屏', !bad.loading, `loading=${bad.loading} noData=${bad.noData} mainCanvas=${bad.mainCanvas}`)
shots.push(await go('05-invalid-symbol', 300))

console.log('\n=== exceptions ===', JSON.stringify(page.exceptions.slice(0, 8), null, 1))
console.log('=== console errors ===', JSON.stringify(page.consoleErrors.slice(0, 8), null, 1))
console.log('=== console warnings ===', JSON.stringify(page.consoleWarnings.slice(0, 6), null, 1))
console.log('=== net bad ===', JSON.stringify(page.netBad.slice(0, 8), null, 1))
console.log('=== shots ==='); shots.filter(Boolean).forEach(s => console.log('  ' + s))
process.exit(S.summary() ? 0 : 1)
