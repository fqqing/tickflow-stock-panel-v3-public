/**
 * 缠论叠加层对拍脚本（P1 收尾）。
 *
 * 目的：ECharts 与 KLineChart 两个内核消费的是**同一份 IR**（`buildChanOverlay` 产出的
 * polylines / ranges / markers）。只要 IR 契约不变，两个内核画出来的几何就一定一致。
 * 本脚本把这份契约用断言锁死，避免以后改 chan-overlay 时静默跑偏。
 *
 * 对什么拍：
 *   1. 计数   —— 笔 / 中枢 / 买卖点的数量（含图外丢弃）
 *   2. 逐笔   —— 每笔的两个端点（date + price），含越界裁剪后的线性插值
 *   3. 中枢   —— ZG/ZD 水平线与色块区间，含裁剪
 *   4. 稠密化 —— ECharts 侧用 densePolyline 把「两个端点」铺成逐 bar 的数组，
 *                必须严格等于端点之间的线性插值，段外必须是 '-'
 *                （KLineChart 侧直接用两个端点画直线，不做稠密化）
 *
 * 为什么「一个画逐 bar 直线、一个画端点直线」还是同一条线：
 *   两个内核的 x 轴都是**等距索引轴**（ECharts 是 category 轴；KLineChart 按数据下标
 *   均匀排布，不按 timestamp 留空隙 —— 已在 docs/kline-lab 里实测相邻 bar 像素间距一致）。
 *   所以「索引空间插值」与「像素空间插值」等价，两端点连线重合。
 *
 * 运行：pnpm parity（= vite SSR 打包 + node 执行）
 */
import { buildChanOverlay, EMPTY_CHAN_OVERLAY } from '@/lib/chan-overlay'
import { densePolyline, POLYLINE_GAP } from '@/lib/chart-polyline'
import type { ChanAnalysis } from '@/lib/api'

// ── 断言小工具 ────────────────────────────────────────────────
let pass = 0
const fails: string[] = []

function ok(cond: boolean, name: string, detail = '') {
  if (cond) {
    pass++
    console.log(`  PASS  ${name}`)
  } else {
    fails.push(`${name}${detail ? ` — ${detail}` : ''}`)
    console.log(`  FAIL  ${name}${detail ? ` — ${detail}` : ''}`)
  }
}
function near(a: number, b: number, eps = 1e-9) {
  return Math.abs(a - b) <= eps
}

// ── 构造交易日序列 ────────────────────────────────────────────
function businessDays(startISO: string, n: number): string[] {
  const out: string[] = []
  const d = new Date(`${startISO}T00:00:00Z`)
  while (out.length < n) {
    const wd = d.getUTCDay()
    if (wd !== 0 && wd !== 6) out.push(d.toISOString().slice(0, 10))
    d.setUTCDate(d.getUTCDate() + 1)
  }
  return out
}

/** 缠论计算窗口 100 根；图表只显示其中的 40..79（连续子段），用来触发裁剪 */
const CHAN_DATES = businessDays('2024-01-01', 100)
const CHART_DATES = CHAN_DATES.slice(40, 80)
const LEFT = 40
const RIGHT = 79

const analysis: ChanAnalysis = {
  symbol: 'TEST',
  name: '对拍样本',
  bars: 100,
  dates: CHAN_DATES,
  trend: 'up',
  snapshot: {} as ChanAnalysis['snapshot'],
  sell_snapshot: {} as ChanAnalysis['snapshot'],
  strokes: [
    // 完全在图内 —— 端点不应被改动
    { start_index: 45, end_index: 60, start_date: CHAN_DATES[45], end_date: CHAN_DATES[60], start_price: 10, end_price: 14, direction: 1 },
    // 左端越界 —— 起点裁剪到图左边界并按比例插值
    { start_index: 10, end_index: 50, start_date: CHAN_DATES[10], end_date: CHAN_DATES[50], start_price: 20, end_price: 12, direction: -1 },
    // 右端越界 —— 终点裁剪到图右边界
    { start_index: 70, end_index: 95, start_date: CHAN_DATES[70], end_date: CHAN_DATES[95], start_price: 12, end_price: 18, direction: 1 },
    // 完全在图外 —— 整条丢弃
    { start_index: 0, end_index: 20, start_date: CHAN_DATES[0], end_date: CHAN_DATES[20], start_price: 5, end_price: 6, direction: 1 },
  ],
  centers: [
    { start_index: 30, end_index: 55, start_date: CHAN_DATES[30], end_date: CHAN_DATES[55], zd: 9, zg: 11, stroke_count: 3 },
    { start_index: 60, end_index: 75, start_date: CHAN_DATES[60], end_date: CHAN_DATES[75], zd: 12.5, zg: 13.5, stroke_count: 3 },
  ],
  signals: [
    { kind: 'buy1', label: '一买', is_buy: true, index: 20, date: CHAN_DATES[20], price: 5, divergence: false, center_zd: null, center_zg: null },
    { kind: 'buy2', label: '二买', is_buy: true, index: 50, date: CHAN_DATES[50], price: 11, divergence: false, center_zd: null, center_zg: null },
    { kind: 'buy3', label: '三买', is_buy: true, index: 50, date: CHAN_DATES[50], price: 11.2, divergence: false, center_zd: null, center_zg: null },
    { kind: 'sell1', label: '一卖', is_buy: false, index: 65, date: CHAN_DATES[65], price: 15, divergence: false, center_zd: null, center_zg: null },
  ],
  counts: {},
}

// ── 期望值（手算，避免与实现同构） ─────────────────────────────
// 左裁剪笔: 20 -> 12 over [10,50], 取 index 40
const LEFT_CLIP_PRICE = 20 + ((12 - 20) * (LEFT - 10)) / (50 - 10) // 14
// 右裁剪笔: 12 -> 18 over [70,95], 取 index 79
const RIGHT_CLIP_PRICE = 12 + ((18 - 12) * (RIGHT - 70)) / (95 - 70) // 14.16

console.log('\n=== 缠论叠加层对拍 ===')
console.log(`缠论窗口 100 根, 图表窗口 ${CHART_DATES.length} 根 (${CHART_DATES[0]} ~ ${CHART_DATES[CHART_DATES.length - 1]})`)

// 1. 空输入
{
  const empty = buildChanOverlay(null, CHART_DATES)
  ok(empty === EMPTY_CHAN_OVERLAY, '空分析返回稳定的 EMPTY 引用（useMemo 依赖比较要靠它）')
  ok(buildChanOverlay(analysis, []).polylines.length === 0, '图表日期序列为空时不产出任何图层')
}

// 2. 计数
const layers = buildChanOverlay(analysis, CHART_DATES)
ok(layers.polylines.filter(p => p.name.startsWith('chan-stroke')).length === 3, '笔数量 = 3（完全在图外的那条被丢弃）')
ok(layers.ranges.length === 2, '中枢色块数量 = 2')
ok(layers.polylines.filter(p => p.name.startsWith('chan-center')).length === 4, '中枢 ZG/ZD 水平线 = 2 中枢 × 2 条')
ok(layers.markers.length === 2, '买卖点 = 2（图外的丢弃 + 同日两个买点合并）')

// 3. 逐笔端点
{
  const strokes = layers.polylines.filter(p => p.name.startsWith('chan-stroke'))
  const inside = strokes[0]
  const left = strokes[1]
  const right = strokes[2]
  ok(inside.points[0].date === CHAN_DATES[45] && inside.points[1].date === CHAN_DATES[60], '图内笔端点日期不被改动')
  ok(near(inside.points[0].price, 10) && near(inside.points[1].price, 14), '图内笔端点价格不被改动')

  ok(left.points[0].date === CHART_DATES[0], '左越界笔的起点被裁剪到图左边界', left.points[0].date)
  ok(near(left.points[0].price, LEFT_CLIP_PRICE), `左越界笔起点按线性插值 = ${LEFT_CLIP_PRICE}`, String(left.points[0].price))
  ok(left.points[1].date === CHAN_DATES[50], '左越界笔的终点保持在图内')

  ok(right.points[1].date === CHART_DATES[CHART_DATES.length - 1], '右越界笔的终点被裁剪到图右边界', right.points[1].date)
  ok(near(right.points[1].price, RIGHT_CLIP_PRICE), `右越界笔终点按线性插值 = ${RIGHT_CLIP_PRICE}`, String(right.points[1].price))

  ok(inside.name === 'chan-stroke-up' && inside.color === '#F87171', '向上笔红 (#F87171)')
  ok(left.name === 'chan-stroke-down' && left.color === '#34D399', '向下笔绿 (#34D399)')
  ok(strokes.every(s => s.points.length === 2), '每笔只有 2 个顶点（已去稠密化, 稠密化交给内核适配器）')
}

// 4. 中枢
{
  ok(layers.ranges[0].start === CHART_DATES[0] && layers.ranges[0].end === CHAN_DATES[55], '左越界中枢的起点被裁剪到图左边界')
  ok(layers.ranges[1].start === CHAN_DATES[60] && layers.ranges[1].end === CHAN_DATES[75], '图内中枢区间不被改动')
  ok(layers.ranges[0].label === undefined && layers.ranges[1].label === '中枢', '只给最后一个中枢打标签（历史中枢全标会糊成一片）')
  const zgLines = layers.polylines.filter(p => p.name.startsWith('chan-center-zg'))
  const zdLines = layers.polylines.filter(p => p.name.startsWith('chan-center-zd'))
  ok(near(zgLines[0].points[0].price, 11) && near(zdLines[0].points[0].price, 9), '中枢 ZG/ZD 水平线取精确价位')
  ok(zgLines.every(l => l.dashed === true), '中枢上下沿是虚线')
}

// 5. 买卖点
{
  const buy = layers.markers.find(m => m.kind === 'buy')
  const sell = layers.markers.find(m => m.kind === 'sell')
  ok(!!buy && buy.date === CHAN_DATES[50], '图内买点日期正确')
  ok(!!buy && buy.label === '二买、三买', '同一根 K 线上的多个买点合并成一个标签', buy?.label)
  ok(!!sell && sell.date === CHAN_DATES[65], '图内卖点日期正确')
  ok(layers.markers.every(m => m.date !== CHAN_DATES[20]), '图外买卖点被丢弃')
}

// 6. y 轴契约
ok(layers.priceLines.length === 0, 'priceLines 恒为空（中枢 ZG/ZD 走折线, 不并入 y 轴 range 免得拉爆纵轴）')

// 7. 稠密化对拍：ECharts 侧把两个端点铺成逐 bar 数组
{
  const indexOf = new Map(CHART_DATES.map((d, i) => [d, i]))
  const strokes = layers.polylines.filter(p => p.name.startsWith('chan-stroke'))
  let allMatch = true
  let gapMatch = true
  let filled = 0
  for (const s of strokes) {
    const dense = densePolyline(s.points, CHART_DATES.length, indexOf)
    const ia = indexOf.get(s.points[0].date)
    const ib = indexOf.get(s.points[1].date)
    if (ia == null || ib == null) { allMatch = false; continue }
    for (let i = 0; i < dense.length; i++) {
      if (i < ia || i > ib) {
        if (dense[i] !== POLYLINE_GAP) gapMatch = false
        continue
      }
      const t = (i - ia) / (ib - ia)
      const expect = s.points[0].price + (s.points[1].price - s.points[0].price) * t
      const got = dense[i]
      filled++
      if (typeof got !== 'number' || !near(got, expect, 1e-9)) allMatch = false
    }
  }
  ok(allMatch, `稠密化后每个 bar 的价格 == 两端点线性插值（共 ${filled} 个采样点）`)
  ok(gapMatch, '稠密化后段外一律是断点占位符（不会从图外拉一条线进来）')
}

// 8. 子图层开关
{
  const noStroke = buildChanOverlay(analysis, CHART_DATES, { strokes: false })
  ok(noStroke.polylines.filter(p => p.name.startsWith('chan-stroke')).length === 0, 'strokes:false 只关笔')
  ok(noStroke.ranges.length === 2 && noStroke.markers.length === 2, 'strokes:false 不影响中枢与买卖点')
  const noCenter = buildChanOverlay(analysis, CHART_DATES, { centers: false })
  ok(noCenter.ranges.length === 0 && noCenter.polylines.filter(p => p.name.startsWith('chan-center')).length === 0, 'centers:false 关掉色块与 ZG/ZD 线')
  const noSignal = buildChanOverlay(analysis, CHART_DATES, { signals: false })
  ok(noSignal.markers.length === 0, 'signals:false 关掉买卖点')
}

console.log(`\n结果: ${pass} 通过 / ${fails.length} 失败`)
if (fails.length > 0) {
  for (const f of fails) console.log(`  × ${f}`)
  process.exit(1)
}
console.log('对拍通过：两个内核消费同一份 IR, 几何一致。')
