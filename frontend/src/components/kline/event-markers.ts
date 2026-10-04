/**
 * 外部事件标记 → KLineChart overlay (监控触发 / 回测买卖点)。
 *
 * 与 signal-markers 的区别: 信号是从 KLineData 里读的(本来就在行上), 而外部
 * 事件是异步取来的另一份数据, 不在 K 线行里。若把它们塞进 KLineData, 每次
 * 开关都要重建整个 dataList —— 而开关恰恰是用户最常点的动作。所以这里用
 * WeakMap 按 chart 实例挂数据, overlay 绘制时按日期反查, 不动 dataList。
 *
 * 日期匹配: KLineData.timestamp 是 UTC epoch, 日K存当天 UTC 00:00
 * (见 KLinePro.parseTs), 所以直接取 UTC 日期即可, 不能再 +8h, 否则整点错位。
 *
 * 画法与 signal-markers 保持一致: above 画向上三角在 HIGH 上方, below 画向
 * 下三角在 LOW 下方, 同日多个标记按纵向槽位堆叠。文字只在 bar 间距足够时画。
 */
import * as kc from 'klinecharts'
import type { ChartEventMark } from '@/lib/chart-events'

/** chart 实例 → 日期 → 标记列表。WeakMap 避免图表销毁后残留。 */
const STORE = new WeakMap<object, Map<string, ChartEventMark[]>>()

type Points = { date: string; marks: ChartEventMark[] }[]

export function setEventMarks(chart: object, points: Points | null): void {
  if (!points || points.length === 0) STORE.delete(chart)
  else STORE.set(chart, new Map(points.map((p) => [p.date, p.marks])))
}

export interface EventMarkersPayload {
  /**
   * 数据版本号。overlay 不靠 extendData 取数(数从 WeakMap 读), 但需要一个变化
   * 信号触发 createPointFigures 重跑 —— 事件数据变化时 KLinePro 会改写它。
   */
  rev: string
}

/** timestamp(UTC epoch ms) → UTC 日期 YYYY-MM-DD */
function tsToDate(ts: number): string {
  const d = new Date(ts)
  const y = d.getUTCFullYear()
  const m = String(d.getUTCMonth() + 1).padStart(2, '0')
  const day = String(d.getUTCDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

const MAX_PER_BAR = 4
const SLOT = 13
const TEXT_MIN_BAR_W = 12

export function registerEventMarkersOverlay(): void {
  kc.registerOverlay({
    name: 'eventMarkers',
    totalStep: 1,
    lock: true,
    needDefaultPointFigure: false,
    needDefaultXAxisFigure: false,
    needDefaultYAxisFigure: false,
    createPointFigures: ({ chart, xAxis, bounding }) => {
      const marks = STORE.get(chart)
      const data = chart.getDataList()
      if (!marks || marks.size === 0 || data.length === 0) return []

      // ── 裁到可视区间 ──
      let from = 0
      let to = data.length - 1
      if (xAxis) {
        const left = xAxis.convertTimestampFromPixel(bounding.left)
        const right = xAxis.convertTimestampFromPixel(bounding.right)
        if (left != null && Number.isFinite(left)) {
          while (from < to && data[from].timestamp < left) from += 1
          from = Math.max(0, from - 1)
        }
        if (right != null && Number.isFinite(right)) {
          while (to > from && data[to].timestamp > right) to -= 1
          to = Math.min(data.length - 1, to + 1)
        }
      }

      const P = (ts: number, v: number): kc.Coordinate | null => {
        const c = chart.convertToPixel({ timestamp: ts, value: v }, { paneId: 'candle_pane' })
        if (Array.isArray(c) || c == null || c.x == null || c.y == null) return null
        return { x: c.x, y: c.y }
      }

      let barW = 6
      if (to > from) {
        const a = P(data[from].timestamp, Number(data[from].close))
        const b = P(data[from + 1].timestamp, Number(data[from + 1].close))
        if (a && b) barW = Math.abs(b.x - a.x)
      }
      const showText = barW >= TEXT_MIN_BAR_W

      const figs: kc.OverlayFigure[] = []
      for (let i = from; i <= to; i += 1) {
        const d = data[i]
        const list = marks.get(tsToDate(d.timestamp))
        if (!list || list.length === 0) continue
        const lowP = P(d.timestamp, Number(d.low))
        const highP = P(d.timestamp, Number(d.high))
        if (!lowP || !highP) continue

        let below = 0
        let above = 0
        for (const mk of list.slice(0, MAX_PER_BAR)) {
          const isAbove = mk.side === 'above'
          const slot = isAbove ? above++ : below++
          const p = isAbove ? highP : lowP
          const y = isAbove ? p.y - 6 - slot * SLOT : p.y + 6 + slot * SLOT
          const s = 4.5
          const tipY = isAbove ? y - s : y + s
          figs.push({
            type: 'polygon',
            attrs: {
              coordinates: [
                { x: p.x, y: tipY },
                { x: p.x - s, y },
                { x: p.x + s, y },
              ],
            },
            styles: { style: 'fill', color: mk.color },
          })
          if (showText) {
            figs.push({
              type: 'text',
              attrs: {
                x: p.x,
                y: isAbove ? tipY - 2 : tipY + 2,
                text: mk.label,
                align: 'center',
                baseline: isAbove ? 'bottom' : 'top',
              },
              styles: { color: mk.color, size: 10 },
            })
          }
        }
      }
      return figs
    },
  })
}
