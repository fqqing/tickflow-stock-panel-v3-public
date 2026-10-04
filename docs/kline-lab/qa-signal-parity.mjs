/**
 * 两内核「信号通道」一致性回归。
 *
 * ★ 背景: toOHLC 曾只挑固定字段, signal_* 列被整列丢掉 —— 于是 ECharts 内核下
 *   事件时间轴的「信号」恒为 0 条, 而 KLinePro 同一窗口是 126 条。这类「某个内核
 *   悄悄少数据」的缺陷不会抛错, 只会让功能静默失效, 只能靠断言锁住。
 *
 * 断言(同一标的、同一默认窗口、都打开事件时间轴):
 *   ① 两内核的 daily 请求都带上了 signal_* 字段
 *   ② 两内核读出的时间轴信号数相等, 且 > 0
 *
 * 用法: node qa-signal-parity.mjs [SYMBOL]
 */
import { openPage, sleep } from './qa-harness.mjs'

const BASE = 'http://127.0.0.1:3011'
const SYM = process.argv[2] || '000001.SZ'

const page = await openPage()
await page.resize(1600, 950)

const lastKline = () => page.eval(`(() => {
  const rs = performance.getEntriesByType('resource').filter(e => /\\/api\\/kline\\/daily/.test(e.name))
  const u = rs.length ? rs[rs.length - 1].name : ''
  const f = decodeURIComponent((u.match(/fields=([^&]*)/) || [])[1] || '')
  const sig = f.split(',').filter(x => /^signal_/.test(x))
  return { n: rs.length, sigN: sig.length }
})()`)

const timelineN = () => page.eval(`(() => {
  const m = (document.body.innerText || '').match(/信号\\s*(\\d+)/)
  return m ? +m[1] : -1
})()`)

const out = {}
for (const k of ['echarts', 'klinepro']) {
  await page.setKernel(k)
  await page.goto(BASE + '/stock/' + encodeURIComponent(SYM))
  await sleep(7000)
  const before = await lastKline()
  await page.key('e')
  await sleep(6000)
  const after = await lastKline()
  const n = await timelineN()
  out[k] = { sigN: after.sigN, n }
  console.log('### ' + k + ' ###')
  console.log('  打开时间轴前: 请求数=' + before.n + ' signal字段=' + before.sigN)
  console.log('  打开时间轴后: 请求数=' + after.n + ' signal字段=' + after.sigN + '  时间轴信号数=' + n)
}

const items = [
  ['两内核 daily 请求都带 signal_* 字段',
    out.echarts.sigN > 0 && out.klinepro.sigN > 0,
    `echarts=${out.echarts.sigN} klinepro=${out.klinepro.sigN}`],
  ['两内核时间轴信号数一致且 > 0',
    out.echarts.n > 0 && out.echarts.n === out.klinepro.n,
    `echarts=${out.echarts.n} klinepro=${out.klinepro.n}`],
]
let fail = 0
for (const [name, ok, detail] of items) {
  if (!ok) fail++
  console.log(`  ${ok ? '[PASS]' : '[FAIL]'} ${name}  ${detail}`)
}
console.log(`\n[信号通道一致性] 通过 ${items.length - fail} / 失败 ${fail}`)
process.exit(fail)
