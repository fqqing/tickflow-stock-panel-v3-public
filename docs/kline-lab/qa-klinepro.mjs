/**
 * QA: KLinePro(klinecharts) 内核功能测试。
 *   node qa-klinepro.mjs [symbol]
 *
 * 覆盖: 内核切换与状态继承 / 主图副图指标 / 指标面板 / 缠论 / 时间轴+定位 / 快捷键 / 响应式
 */
import { openPage, sleep, Suite } from './qa-harness.mjs'

const SYM = process.argv[2] || '000001.SZ'
const BASE = 'http://127.0.0.1:3011'
const HERE = 'D:/project/GP/tickflow-stock-panel/docs/kline-lab/'
const page = await openPage()
const S = new Suite('KLinePro 内核')

const btns = () => page.eval(`[...document.querySelectorAll('button')]
  .filter(b => b.getBoundingClientRect().top < 110)
  .map(b => ({ t: (b.innerText||'').trim().replace(/\\n/g,'/'), ti: (b.title||'').slice(0,30) }))
  .filter(b => b.t || b.ti)`)

const clickByText = async (label, loose = false) => {
  const r = await page.eval(`(() => {
    const all = [...document.querySelectorAll('button')]
    const hit = all.find(x => {
      const t = (x.innerText||'').trim()
      return ${loose ? `t.startsWith(${JSON.stringify(label)})` : `t === ${JSON.stringify(label)}`}
    })
    if (!hit) return 'not-found'
    hit.click(); return 'clicked'
  })()`)
  if (r === 'not-found') console.log('    ! 未找到按钮', JSON.stringify(label), '现有:', JSON.stringify((await btns()).map(b => b.t)))
  return r
}
const clickByTitle = async frag => page.eval(`(() => {
  const b = [...document.querySelectorAll('button')].find(x => (x.title||'').includes(${JSON.stringify(frag)}))
  if (!b) return 'not-found'
  b.click(); return 'clicked'
})()`)

const crashed = async () => String(await page.eval('(document.body.innerText||"")')).includes('Unexpected Application Error')

const snap = async (tag, ms = 2500) => { await sleep(ms); return page.shot(HERE + `qa-klp-${tag}.png`) }
const canvasCount = () => page.eval(`document.querySelectorAll('canvas').length`)
const infoBar = () => page.eval(`(() => {
  const t = document.body.innerText || ''
  const ma = (t.match(/MA\\d+: ?[\\d.]+/g) || []).slice(0, 6)
  const sub = (t.match(/(MACD|KDJ|RSI|BOLL|VOL|WR|CCI|BIAS|DMI|OBV)[^\\n]{0,30}/g) || []).slice(0, 5)
  return { ma, sub, noData: t.includes('暂无该周期数据'), loading: t.includes('载入中') }
})()`)

// ── 准备 ──
await page.resize(1600, 950)
await page.send('Page.stopLoading').catch(() => {})
await page.goto(`${BASE}/stock/NONE.XX`)            // 先离开旧页面, 断开可能卡住的加载
await sleep(800)
await page.setKernel('klinepro')
await page.eval("localStorage.removeItem('tf-kline-indicators')")
const landed = await page.goto(`${BASE}/stock/${encodeURIComponent(SYM)}`)
const at = await page.currentSymbol()
S.ok('K0_导航落在目标标的上', landed && at === SYM, `landed=${landed} symbol=${at}`)
await sleep(6500)

const k = await page.kernel()
S.ok('K1_锁定内核为KLinePro', k === 'KLinePro', `kernel=${k}`)
let cv0 = await canvasCount()
let ib = await infoBar()
await snap('01-baseline', 400)
const ink = await page.inkRatio()
console.log('  baseline canvases=' + cv0, 'ink=' + ink, JSON.stringify(ib))
// klinecharts 的指标文字画在 canvas 里, DOM 读不到 —— 只能验「画面上真的有内容」
S.ok('K2_主图有实际绘制内容', ink > 0.005, `主图油墨占比=${ink}`)
S.ok('K3_副图已渲染', cv0 >= 3, `canvases=${cv0}`)
S.ok('K4_无加载遮罩', !ib.loading, `loading=${ib.loading}`)

// ── 指标管理面板 ──
page.reset()
const openInd = await clickByText('指标', true)
await sleep(1200)
const panel = await page.eval(`(() => {
  const t = document.body.innerText || ''
  const addBtns = [...document.querySelectorAll('button')].filter(b => b.title && /^[A-Z]{2,8}$/.test(b.title))
  return {
    hasGroups: t.includes('添加主图') && t.includes('添加副图'),
    addables: addBtns.map(b => b.title),
    empty: t.includes('未添加任何指标'),
  }
})()`)
await snap('02-indicator-panel', 300)
S.ok('K5_指标面板可打开', openInd === 'clicked' && panel.hasGroups && panel.addables.length >= 10,
  `click=${openInd} 可添加指标=${panel.addables.length} 组=${panel.hasGroups} 空态=${panel.empty}`)
S.note('K5b_可添加指标清单', JSON.stringify(panel.addables))

// 添加一个副图指标 MACD: 面板里每个添加按钮的 title 就是指标英文名
const addInd = await page.eval(`(() => {
  const b = [...document.querySelectorAll('button')].find(x => x.title === 'MACD')
    || [...document.querySelectorAll('button')].find(x => x.title === 'KDJ')
  if (!b) return 'not-found'
  b.click(); return 'clicked:' + b.title
})()`)
await sleep(3000)
const cvAfterInd = await canvasCount()
const ibAfterInd = await infoBar()
await snap('03-after-add-macd', 300)
S.ok('K6_可添加副图指标MACD', addInd.startsWith('clicked') && cvAfterInd > cv0 + 0,
  `action=${addInd} canvases ${cv0} -> ${cvAfterInd} sub=${JSON.stringify(ibAfterInd.sub)}`)
S.note('K6b_MACD是否真的出现', `副图文本=${JSON.stringify(ibAfterInd.sub)}`)

// 关面板
await page.key('Escape'); await sleep(600)

// ── 缠论叠加 ──
page.reset()
const cvB = await page.canvasDigest()
const chanClick = await clickByTitle('缠论笔')
await sleep(3500)
const cvA = await page.canvasDigest()
await snap('04-chan')
S.ok('K7_缠论叠加无异常', chanClick === 'clicked' && !(await crashed()), `click=${chanClick} exc=${page.exceptions.length}`)
S.ok('K7b_缠论画面有变化', cvB !== cvA, `${cvB} -> ${cvA}`)

// ── 周期切换 ──
page.reset()
const periods = [['周', 'week'], ['月', 'month'], ['60分', 'm60'], ['日', 'day']]
let periodFails = []
for (const [label, tag] of periods) {
  const r = await clickByText(label)
  await sleep(3000)
  const st = await infoBar()
  await snap(`05-period-${tag}`, 200)
  const bad = r !== 'clicked' || st.noData || st.loading || page.exceptions.length || (await crashed())
  if (bad) periodFails.push(`${label}:click=${r},noData=${st.noData},exc=${page.exceptions.length}`)
}
S.ok('K8_周期切换全部正常', periodFails.length === 0, periodFails.join(' | ') || '4/4 ok')

// ── 时间轴(日线档应出现条带) ──
page.reset()
const tlClick = await clickByTitle('事件时间轴')
await sleep(3500)
const tlInfo = await page.eval(`(() => {
  const t = document.body.innerText || ''
  // 时间轴条带: 找含日期或事件文字的横向窄条
  const strip = [...document.querySelectorAll('div')].filter(d => d.clientHeight > 12 && d.clientHeight < 90 && d.clientWidth > 300
    && /信号|触发|买卖点|全部|日内/.test(d.innerText || ''))
  return { stripCount: strip.length, sample: strip.slice(0,3).map(d => (d.innerText||'').trim().replace(/\\n/g,'|').slice(0,70)) }
})()`)
await snap('06-timeline')
S.ok('K9_时间轴开启无异常', tlClick === 'clicked' && !(await crashed()), `click=${tlClick} exc=${page.exceptions.length}`)
S.ok('K9b_日线档时间轴有条带', tlInfo.stripCount > 0, JSON.stringify(tlInfo))

// 点击条带定位
if (tlInfo.stripCount > 0) {
  page.reset()
  const clicked = await page.eval(`(() => {
    const strip = [...document.querySelectorAll('div')].find(d => d.clientHeight > 12 && d.clientHeight < 90 && d.clientWidth > 300 && /信号|触发|买卖点/.test(d.innerText||''))
    if (!strip) return 'no-strip'
    const marks = [...strip.querySelectorAll('*')].filter(e => {
      const r = e.getBoundingClientRect()
      return r.width <= 6 && r.height > 6
    })
    if (!marks.length) return 'no-mark(' + strip.children.length + ')'
    marks[Math.floor(marks.length/2)].dispatchEvent(new MouseEvent('click', { bubbles: true }))
    return 'clicked-mark'
  })()`)
  await sleep(2500)
  await snap('07-timeline-focus')
  S.ok('K10_点击时间轴定位无异常', !(await crashed()) && page.exceptions.length === 0, `action=${clicked} exc=${page.exceptions.length}`)
  S.note('K10b_定位动作', clicked)
}

// ── 快捷键 ──
page.reset()
const keys = [['x', '信号'], ['a', '触发'], ['t', '回测'], ['c', '缠论'], ['s', '定量结构'], ['e', '时间轴']]
const keyIssues = []
for (const [kk] of keys) {
  await page.key(kk)
  await sleep(1800)
  if (await crashed() || page.exceptions.length) keyIssues.push(`${kk}:exc=${page.exceptions.length}`)
}
S.ok('K11_快捷键全部无异常', keyIssues.length === 0, keyIssues.join('|') || '6/6 ok')
await snap('08-shortcuts', 500)

// ── 响应式 ──
const respIssues = []
for (const w of [1280, 1024, 900]) {
  await page.resize(w, 900)
  await sleep(1200)
  const m = await page.eval(`({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth })`)
  await snap(`09-resp-${w}`, 200)
  if (m.sw > m.cw + 1) respIssues.push(`${w}:${m.sw}>${m.cw}`)
  if (await crashed()) respIssues.push(`${w}:crashed`)
}
S.ok('K12_三档宽度无横向滚动/不崩', respIssues.length === 0, respIssues.join('|') || 'ok')

console.log('\n=== exceptions ===', JSON.stringify(page.exceptions.slice(0, 6), null, 1))
console.log('=== console errors ===', JSON.stringify(page.consoleErrors.slice(0, 8), null, 1))
console.log('=== console warnings ===', JSON.stringify([...new Set(page.consoleWarnings)].slice(0, 8), null, 1))
console.log('=== net bad ===', JSON.stringify(page.netBad.slice(0, 8), null, 1))
process.exit(S.summary() ? 0 : 1)
