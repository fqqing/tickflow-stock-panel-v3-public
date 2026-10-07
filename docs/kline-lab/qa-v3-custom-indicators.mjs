/**
 * v3 自定义指标渲染 QA（资金动能 / MACD定量结构 / 蛟龙出海）。
 * 验证：registerIndicator 注册 + 指标面板登记 + 实际 canvas 渲染 + 无 console 错误。
 */
import { openPage, Suite, sleep } from './qa-harness.mjs'

const page = await openPage()
const suite = new Suite('v3 自定义指标渲染')

const SYMBOL = '600522.SH'

// 1. 导航到个股终端
const landed = await page.goto(`http://127.0.0.1:3011/stock/${SYMBOL}`, 15000)
suite.ok('导航到个股终端', landed)
await page.resize(1600, 900)
await sleep(3500)

// 2. 打开指标面板，验证三个自定义指标已登记
await page.eval(`(() => {
  const b = [...document.querySelectorAll('button')].find(x => (x.innerText || '').includes('指标'))
  if (b) b.click()
})()`)
await sleep(1000)
const panelText = await page.eval('document.body.innerText')
suite.ok('指标面板含「资金动能」', panelText.includes('资金动能'))
suite.ok('指标面板含「MACD定量结构」', panelText.includes('MACD定量结构'))
suite.ok('指标面板含「蛟龙出海」', panelText.includes('蛟龙出海'))

// 3. 关闭面板（点空白处或 ESC），然后用 localStorage 预设指标列表 + reload
await page.eval(`(() => {
  const b = [...document.querySelectorAll('button')].find(x => (x.innerText || '').includes('指标'))
  if (b) b.click()
})()`)
await sleep(500)

const preset = [
  { key: 'ma#1', name: 'MA', group: 'main', params: [] },
  { key: 'vol#1', name: 'VOL', group: 'sub', params: [] },
  { key: 'momentum#1', name: 'MOMENTUM', group: 'sub', params: [] },
  { key: 'macd_quant#1', name: 'MACD_QUANT', group: 'sub', params: [] },
  { key: 'dragon#1', name: 'DRAGON', group: 'main', params: [] },
]
await page.eval(`localStorage.setItem('tickflow.kline.indicators.v1', ${JSON.stringify(JSON.stringify(preset))})`)
const digBefore = await page.canvasDigest()
await page.reload()
await sleep(6000)

// 4. 验证渲染：canvas 有内容、加指标后画面指纹变化、副图数量
const digAfter = await page.canvasDigest()
const ink = await page.inkRatio()
suite.ok('canvas 有内容(墨水比例 > 0.01)', ink > 0.01, `ink=${ink}`)
suite.ok('加指标后画面指纹变化', digBefore !== digAfter, `${digBefore} -> ${digAfter}`)

// 5. 检查指标是否真的创建（通过指标面板计数 + 副图 pane 数量）
await page.eval(`(() => {
  const b = [...document.querySelectorAll('button')].find(x => (x.innerText || '').includes('指标'))
  if (b) b.click()
})()`)
await sleep(900)
const indicatorBadge = await page.eval(`(() => {
  const b = [...document.querySelectorAll('button')].find(x => (x.innerText || '').includes('指标'))
  return b ? b.innerText.trim() : ''
})()`)
suite.ok('指标计数 ≥ 5(MA+VOL+3自定义)', /指标\s*5/.test(indicatorBadge), `badge="${indicatorBadge}"`)

// 6. 截图（含三个指标的副图 + 主图生命线）
await page.shot('qa3-custom-indicators.png')

// 7. console 错误
const errs = page.consoleErrors.filter(e => !/React DevTools|Download the React DevTools/i.test(e))
suite.ok('无 console 错误', errs.length === 0, errs.slice(0, 3).join(' | '))
suite.ok('无 JS 异常', page.exceptions.length === 0, page.exceptions.slice(0, 3).join(' | '))

suite.summary()
process.exit(0)
