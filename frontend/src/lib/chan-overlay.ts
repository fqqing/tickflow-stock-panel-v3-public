/**
 * 缠论叠加层构建器：把 `/api/chan/analysis` 的结果转成 K 线图的原语。
 *
 * 映射关系：
 *   笔    → ChartPolyline（相邻顶点之间逐 bar 线性插值 ⇒ 笔直的斜线），向上笔红、向下笔绿
 *   中枢  → ChartRange（色块）+ 2 条水平 ChartPolyline（ZG/ZD 精确价位）
 *   买卖点 → ChartMarker（买点在 low 下方向上箭头，卖点在 high 上方向下箭头）
 *
 * 为什么 中枢上下沿不用 ChartPriceLine：
 *   EChartsCandlestick 会把 priceLines 的 value 全部并入 y 轴 range（axisMin/axisMax），
 *   历史中枢的 ZG/ZD 离现价很远，会把纵轴拉爆。用折线画价位则不会。
 *
 * 图外裁剪：图表日期序列是缠论日期序列的连续子段，落在图外的端点裁剪到图边界并按
 * 笔的两个端点线性插值价格，避免因为端点在图外而整条笔消失。
 */
import type { ChanAnalysis, ChanStrokePoint } from '@/lib/api'
import type { ChartMarker, ChartPolyline, ChartPriceLine, ChartRange } from '@/lib/chart-primitives'

const STROKE_UP_COLOR = '#F87171'
const STROKE_DOWN_COLOR = '#34D399'
const CENTER_FILL = 'rgba(59,130,246,0.10)'
const CENTER_EDGE = '#60A5FA'
const CENTER_EDGE_STRONG = '#3B82F6'

/** 笔/中枢数量上限：正常 400 根 K 线只有 30 笔 / 8 中枢，这里是极端保护 */
const MAX_STROKES = 400
const MAX_CENTERS = 60

export interface ChanOverlayOptions {
  strokes?: boolean
  centers?: boolean
  signals?: boolean
}

export interface ChanOverlayLayers {
  markers: ChartMarker[]
  ranges: ChartRange[]
  priceLines: ChartPriceLine[]
  polylines: ChartPolyline[]
}

/** 稳定的空层引用：便于 useMemo / ECharts setOption 的依赖比较 */
export const EMPTY_CHAN_OVERLAY: ChanOverlayLayers = {
  markers: [],
  ranges: [],
  priceLines: [],
  polylines: [],
}

/** 取笔上某个缠论索引处的价格（按两个端点线性插值） */
function priceAt(stroke: ChanStrokePoint, chanIndex: number): number {
  const span = stroke.end_index - stroke.start_index
  if (span <= 0) return stroke.end_price
  const t = Math.min(Math.max((chanIndex - stroke.start_index) / span, 0), 1)
  return stroke.start_price + (stroke.end_price - stroke.start_price) * t
}

export function buildChanOverlay(
  analysis: ChanAnalysis | null | undefined,
  chartDates: string[],
  options: ChanOverlayOptions = {},
): ChanOverlayLayers {
  if (!analysis || chartDates.length === 0) return EMPTY_CHAN_OVERLAY

  const showStrokes = options.strokes !== false
  const showCenters = options.centers !== false
  const showSignals = options.signals !== false

  const chartSet = new Set(chartDates)
  const leftDate = chartDates[0]
  const rightDate = chartDates[chartDates.length - 1]

  // 图表端点对应的缠论索引（越界用 ±Infinity ⇒ 不裁剪，靠 chartSet 过滤掉图外顶点）
  const chanIndexByDate = new Map<string, number>()
  analysis.dates.forEach((d, i) => chanIndexByDate.set(d, i))
  const chanLeft = chanIndexByDate.get(leftDate)
  const chanRight = chanIndexByDate.get(rightDate)

  const polylines: ChartPolyline[] = []
  const ranges: ChartRange[] = []

  // ── 笔 ────────────────────────────────────────────────────────
  if (showStrokes) {
    for (const stroke of analysis.strokes) {
      if (polylines.length >= MAX_STROKES) break
      let startDate = stroke.start_date
      let startPrice = stroke.start_price
      let endDate = stroke.end_date
      let endPrice = stroke.end_price
      if (chanLeft != null && stroke.start_index < chanLeft) {
        startDate = leftDate
        startPrice = priceAt(stroke, chanLeft)
      }
      if (chanRight != null && stroke.end_index > chanRight) {
        endDate = rightDate
        endPrice = priceAt(stroke, chanRight)
      }
      if (!startDate || !endDate || startDate === endDate) continue
      if (!chartSet.has(startDate) || !chartSet.has(endDate)) continue
      const up = stroke.direction > 0
      polylines.push({
        name: up ? 'chan-stroke-up' : 'chan-stroke-down',
        points: [
          { date: startDate, price: startPrice },
          { date: endDate, price: endPrice },
        ],
        color: up ? STROKE_UP_COLOR : STROKE_DOWN_COLOR,
        width: 1.2,
      })
    }
  }

  // ── 中枢 ──────────────────────────────────────────────────────
  if (showCenters) {
    const lastCenterPos = analysis.centers.length - 1
    for (let pos = 0; pos < analysis.centers.length; pos++) {
      if (ranges.length >= MAX_CENTERS) break
      const center = analysis.centers[pos]
      let startDate = center.start_date
      let endDate = center.end_date
      if (chanLeft != null && center.start_index < chanLeft) startDate = leftDate
      if (chanRight != null && center.end_index > chanRight) endDate = rightDate
      if (!startDate || !endDate || startDate === endDate) continue
      if (!chartSet.has(startDate) || !chartSet.has(endDate)) continue
      // 只给最后一个中枢加标签：历史中枢全打标签会糊成一片
      ranges.push({ start: startDate, end: endDate, color: CENTER_FILL, label: pos === lastCenterPos ? '中枢' : undefined })
      const edgeColor = pos === lastCenterPos ? CENTER_EDGE_STRONG : CENTER_EDGE
      const withLabel = pos === lastCenterPos
      polylines.push({
        name: 'chan-center-zg',
        points: [{ date: startDate, price: center.zg }, { date: endDate, price: center.zg }],
        color: edgeColor,
        width: 1,
        dashed: true,
      })
      polylines.push({
        name: 'chan-center-zd',
        points: [{ date: startDate, price: center.zd }, { date: endDate, price: center.zd }],
        color: edgeColor,
        width: 1,
        dashed: true,
      })
      if (withLabel) {
        // 用极短的折线把 ZG 价位标注出来（不并入 priceLines，免得拉爆 y 轴）
        polylines[polylines.length - 2].name = `chan-center-zg ${center.zg.toFixed(2)}`
        polylines[polylines.length - 1].name = `chan-center-zd ${center.zd.toFixed(2)}`
      }
    }
  }

  // ── 买卖点 ────────────────────────────────────────────────────
  const markers: ChartMarker[] = []
  if (showSignals) {
    // 同一根 K 线上可能同时出现二买+三买（同一笔的终点），合并成一个标签避免重叠
    const grouped = new Map<string, string[]>()
    const order: string[] = []
    for (const signal of analysis.signals) {
      if (!signal.date || !chartSet.has(signal.date)) continue
      const key = `${signal.date}|${signal.is_buy ? 'b' : 's'}`
      const bucket = grouped.get(key)
      if (bucket) {
        if (!bucket.includes(signal.label)) bucket.push(signal.label)
      } else {
        grouped.set(key, [signal.label])
        order.push(key)
      }
    }
    for (const key of order) {
      const [date, side] = key.split('|')
      const labels = grouped.get(key) ?? []
      markers.push({
        date,
        kind: side === 'b' ? 'buy' : 'sell',
        label: labels.join('、'),
      })
    }
  }

  return { markers, ranges, priceLines: [], polylines }
}
