/**
 * 定点验证 300750.SZ：蛟龙信号圆点 + MACD结构标注文字 实际渲染。
 */
import { openPage, Suite, sleep } from './qa-harness.mjs'

const page = await openPage()
const suite = new Suite('蛟龙信号 + 结构标注渲染(300750)')

const preset = [
  { key: 'ma#1', name: 'MA', group: 'main', params: [] },
  { key: 'vol#1', name: 'VOL', group: 'sub', params: [] },
  { key: 'momentum#1', name: 'MOMENTUM', group: 'sub', params: [] },
  { key: 'macd_quant#1', name: 'MACD_QUANT', group: 'sub', params: [] },
  { key: 'dragon#1', name: 'DRAGON', group: 'main', params: [] },
]

const landed = await page.goto('http://127.0.0.1:3011/stock/300750.SZ', 15000)
suite.ok('导航到 300750', landed)
await page.resize(1600, 900)
await sleep(2500)
await page.eval(`localStorage.setItem('tickflow.kline.indicators.v1', ${JSON.stringify(JSON.stringify(preset))})`)
await page.reload()
await sleep(6500)

await page.shot('qa3-300750-full.png')

// 数据层核对: 该窗口蛟龙信号与结构标注的日期
const hits = await page.eval(`fetch('/api/kline/daily?symbol=300750.SZ&start_date=2026-04-07&end_date=2026-10-07&indicators=trend_dragon,macd_structure&fields=date,td_signal,td_a3,ms_btext,ms_by,ms_ttext,ms_ty').then(r=>r.json()).then(d=>{
  const td=d.rows.filter(x=>x.td_signal).map(x=>x.date)
  const mb=d.rows.filter(x=>x.ms_btext>0).map(x=>x.date+':'+x.ms_btext)
  const mt=d.rows.filter(x=>x.ms_ttext>0).map(x=>x.date+':'+x.ms_ttext)
  return {td, mb, mt}
})`)
console.log('蛟龙信号日:', JSON.stringify(hits.td))
console.log('底结构标注:', JSON.stringify(hits.mb))
console.log('顶结构标注:', JSON.stringify(hits.mt))

const errs = page.consoleErrors.filter(e => !/React DevTools/i.test(e))
suite.ok('无 console 错误', errs.length === 0, errs.slice(0, 3).join(' | '))
suite.ok('无 JS 异常', page.exceptions.length === 0, page.exceptions.slice(0, 3).join(' | '))

suite.summary()
process.exit(0)
