/**
 * QA: 边界与健壮性（多市场 / 主题 / a11y / 压力切换 / 分钟档密度）
 *   node qa-misc.mjs
 */
import { openPage, sleep, Suite } from './qa-harness.mjs'

const BASE = 'http://127.0.0.1:3011'
const HERE = 'D:/project/GP/tickflow-stock-panel/docs/kline-lab/'
const page = await openPage()
const S = new Suite('边界与健壮性')
await page.resize(1600, 950)

const crashed = async () => String(await page.eval('(document.body.innerText||"")')).includes('Unexpected Application Error')
const state = () => page.eval(`(() => {
  const t = document.body.innerText || ''
  return {
    loading: t.includes('载入中'),
    noData: t.includes('暂无该周期数据'),
    emptyHint: t.includes('无数据') || t.includes('暂不支持') || t.includes('不可用'),
    canvas: document.querySelectorAll('canvas').length,
    name: (t.match(/\\n([\\u4e00-\\u9fa5A-Za-z][^\\n]{0,10})\\n/) || [])[1] || '',
    toasts: [...document.querySelectorAll('[role="alert"]')].map(e => (e.innerText||'').trim().slice(0,40)).filter(Boolean),
  }
})()`)

// ── 1. 多市场 ──
const markets = [
  ['000001.SZ', '深主板'],
  ['600519.SH', '沪主板'],
  ['688029.SH', '科创板'],
  ['300750.SZ', '创业板'],
  ['920002.BJ', '北交所'],
  ['00700.HK', '港股'],
  ['AAPL.US', '美股'],
]
console.log('\n===== 多市场 =====')
for (const [sym, label] of markets) {
  page.reset()
  await page.send('Page.stopLoading').catch(() => {})
  await page.setKernel('echarts')
  const ok = await page.goto(`${BASE}/stock/${encodeURIComponent(sym)}`)
  const at = await page.currentSymbol()
  await sleep(6500)
  const st = await state()
  const ink = await page.inkRatio()
  const dead = await crashed()
  await page.shot(HERE + `qa-misc-mkt-${sym.replace('.', '_')}.png`)
  const bad = page.netBad.filter(b => !/depth/.test(b.url))
  const passed = ok && at === sym && !dead && !st.loading && ink > 0.004
  S.ok(`M_${label}_${sym}`, passed,
    `落地=${at} 崩=${dead} loading=${st.loading} 油墨=${ink} canvas=${st.canvas} 非depth4xx=${bad.length} toast=${JSON.stringify(st.toasts.slice(0,1))}`)
  if (bad.length) S.note(`M_${label}_异常请求`, JSON.stringify(bad.slice(0, 3)))
}

// ── 2. 主题切换 ──
console.log('\n===== 主题切换 =====')
page.reset()
await page.send('Page.stopLoading').catch(() => {})
const okBack = await page.goto(`${BASE}/stock/000001.SZ`)
await sleep(6000)
const themeOf = () => page.eval(`document.documentElement.classList.contains('dark') ? 'dark' : 'light'`)
const t0 = await themeOf()
const toggled = await page.eval(`(() => { const b=[...document.querySelectorAll('button')].find(x=>/亮色模式|暗色模式|主题/.test(x.title||'')); if(!b) return 'no-btn'; b.click(); return 'clicked' })()`)
await sleep(2500)
const t1 = await themeOf()
const inkLight = await page.inkRatio()
await page.shot(HERE + 'qa-misc-theme-toggled.png')
S.ok('T_主题可切换', toggled === 'clicked' && t0 !== t1, `${t0} -> ${t1}`)
S.ok('T_切换主题后不崩且仍有图', !(await crashed()) && inkLight > 0.004, `油墨=${inkLight}`)
// 切回
await page.eval(`(() => { const b=[...document.querySelectorAll('button')].find(x=>/亮色模式|暗色模式|主题/.test(x.title||'')); if(b) b.click() })()`)
await sleep(1500)

// ── 3. a11y ──
console.log('\n===== 可访问性 =====')
const a11y = await page.eval(`(() => {
  const btns = [...document.querySelectorAll('button')].filter(b => b.getBoundingClientRect().top >= 0 && b.getBoundingClientRect().top < 200)
  const noLabel = btns.filter(b => !(b.innerText||'').trim() && !b.title && !b.getAttribute('aria-label'))
  const inputs = [...document.querySelectorAll('input')]
  const noInputLabel = inputs.filter(i => !i.getAttribute('aria-label') && !i.placeholder && !i.id)
  const clickable = [...document.querySelectorAll('[onclick], [role="button"]')]
  return {
    btnTotal: btns.length,
    btnNoLabel: noLabel.length,
    inputTotal: inputs.length,
    noInputLabel: noInputLabel.length,
    roleButtons: clickable.filter(e => e.tagName !== 'BUTTON').length,
    hasMain: !!document.querySelector('main'),
    hasH1: !!document.querySelector('h1'),
    lang: document.documentElement.lang || '(无)',
  }
})()`)
console.log('  ', JSON.stringify(a11y))
S.ok('A1_图标按钮都有可读名称', a11y.btnNoLabel === 0, `无名称按钮=${a11y.btnNoLabel}/${a11y.btnTotal}`)
S.ok('A2_有 lang 声明', a11y.lang !== '(无)', `lang=${a11y.lang}`)
S.note('A3_语义结构', `main=${a11y.hasMain} h1=${a11y.hasH1} 非button可点元素=${a11y.roleButtons} 无标签输入框=${a11y.noInputLabel}`)

// 键盘可达性: Tab 走一遍看焦点是否可见
const focusFlow = await page.eval(`(() => {
  const els = [...document.querySelectorAll('button, a, input, [tabindex]')]
    .filter(e => e.offsetParent !== null && !e.disabled)
  const noFocusRing = els.filter(e => {
    const s = getComputedStyle(e)
    return !s.outlineStyle || s.outlineStyle === 'none'
  }).length
  return { tabbable: els.length, noOutline: noFocusRing }
})()`)
S.note('A4_可 Tab 元素', JSON.stringify(focusFlow))

// ── 4. 压力: 连续切内核 ──
console.log('\n===== 压力测试 =====')
page.reset()
for (let i = 0; i < 6; i++) {
  await page.key('g')
  await sleep(1200)
}
await sleep(2000)
const afterToggle = await state()
const deadT = await crashed()
await page.shot(HERE + 'qa-misc-stress-toggle.png')
S.ok('P1_连续切换内核6次不崩', !deadT && afterToggle.canvas > 0 && page.exceptions.length === 0,
  `崩=${deadT} canvas=${afterToggle.canvas} exc=${page.exceptions.length}`)

// 连续切股(自选列表点选)
page.reset()
const symbolsSeen = []
for (let i = 0; i < 5; i++) {
  const r = await page.eval(`(() => {
    const list = [...document.querySelectorAll('button')].filter(b => /^\\d{6}\s/.test((b.innerText||'').replace(/\\n/g,' ')) || /^\\d{6}/.test((b.innerText||'').trim()))
    if (!list.length) return 'none'
    const b = list[${i} % list.length] || list[0]
    b.click(); return b.getAttribute('title') || b.innerText.trim().slice(0,10)
  })()`)
  symbolsSeen.push(r)
  await sleep(2500)
}
await sleep(2500)
const deadS = await crashed()
const afterSwitch = await state()
await page.shot(HERE + 'qa-misc-stress-symbol.png')
S.ok('P2_连续换股5次不崩', !deadS && page.exceptions.length === 0,
  `崩=${deadS} exc=${page.exceptions.length} 切换=${JSON.stringify(symbolsSeen)} canvas=${afterSwitch.canvas} loading=${afterSwitch.loading}`)

console.log('\n=== exceptions ===', JSON.stringify([...new Set(page.exceptions)].slice(0, 8), null, 1))
console.log('=== console errors ===', JSON.stringify([...new Set(page.consoleErrors)].slice(0, 8), null, 1))
console.log('=== console warnings ===', JSON.stringify([...new Set(page.consoleWarnings)].slice(0, 8), null, 1))
console.log('=== net 4xx/5xx ===', JSON.stringify(page.netBad.slice(0, 10), null, 1))
process.exit(S.summary() ? 0 : 1)
