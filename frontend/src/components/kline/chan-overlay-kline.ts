/**
 * 缠论原语 → KLineChart overlay 适配器。
 *
 * 对应 docs/chan-migration-plan.md 的 "registerOverlay + extendData" 路径。
 * 用法：先 registerChanOverlay()，再用 chart.createOverlay({ name:'chan', ... extendData: layers })。
 */
import * as kc from 'klinecharts'
import type { ChartMarker, ChartPolyline, ChartRange } from '@/lib/chart-primitives'

const BULL = '#F04438' // 红涨
const BEAR = '#12B76A' // 绿跌
const CENTER_FILL = 'rgba(59,130,246,0.12)'
const CENTER_EDGE = '#60A5FA'

interface ChanPayload {
  polylines: ChartPolyline[]
  ranges: ChartRange[]
  markers: ChartMarker[]
}

let registered = false

export function registerChanOverlay() {
  if (registered) return
  registered = true

  kc.registerOverlay<ChanPayload>({
    name: 'chan',
    totalStep: 1,
    lock: true,
    needDefaultPointFigure: false,
    needDefaultXAxisFigure: false,
    needDefaultYAxisFigure: false,
    createPointFigures: ({ chart, overlay, bounding }) => {
      const { polylines, ranges, markers } = overlay.extendData ?? {
        polylines: [], ranges: [], markers: [],
      }
      const figs: kc.OverlayFigure[] = []
      const P = (ts: number, v: number): kc.Coordinate | null => {
        const c = chart.convertToPixel({ timestamp: ts, value: v }, { paneId: 'candle_pane' })
        if (Array.isArray(c) || c == null || c.x == null || c.y == null) return null
        return { x: c.x, y: c.y }
      }

      // 笔（斜线）
      for (const line of polylines) {
        if (line.points.length < 2) continue
        const [a, b] = line.points
        const pa = P(new Date(a.date).getTime(), a.price)
        const pb = P(new Date(b.date).getTime(), b.price)
        if (!pa || !pb) continue
        // 完全在可视区外则跳过, 避免巨大坐标撑 canvas
        if ((pa.x < bounding.left && pb.x < bounding.left) || (pa.x > bounding.right && pb.x > bounding.right)) continue
        figs.push({
          type: 'line',
          attrs: { coordinates: [{ x: pa.x, y: pa.y }, { x: pb.x, y: pb.y }] },
          styles: { color: line.color || (line.name?.includes('down') ? BEAR : BULL), size: line.width ?? 1.5, style: 'solid' },
        })
      }

      // 中枢：色块 + ZG/ZD 水平线
      for (const r of ranges) {
        const sTs = new Date(r.start).getTime()
        const eTs = new Date(r.end).getTime()
        // 匹配与中枢同起止日期的两条水平线（ZG/ZD）
        const horizontals = polylines.filter(
          p =>
            p.points.length === 2 &&
            p.points[0].date === r.start &&
            p.points[1].date === r.end,
        )
        if (horizontals.length < 2) continue
        const prices = horizontals.map(p => p.points[0].price).sort((a, b) => a - b)
        const zd = prices[0]
        const zg = prices[prices.length - 1]
        const pa = P(sTs, zg)
        const pb = P(eTs, zd)
        if (!pa || !pb) continue
        const x = Math.min(pa.x, pb.x), w = Math.abs(pb.x - pa.x)
        const y = Math.min(pa.y, pb.y), h = Math.abs(pb.y - pa.y)
        if (w <= 0 || h <= 0) continue
        figs.push({
          type: 'rect',
          attrs: { x, y, width: w, height: h },
          styles: { style: 'fill', color: r.color || CENTER_FILL, borderColor: CENTER_EDGE, borderSize: 1 },
        })
        for (const price of [zg, zd]) {
          const p1 = P(sTs, price), p2 = P(eTs, price)
          if (!p1 || !p2) continue
          figs.push({
            type: 'line',
            attrs: { coordinates: [{ x: p1.x, y: p1.y }, { x: p2.x, y: p2.y }] },
            styles: { color: CENTER_EDGE, size: 1, style: 'dashed', dashedValue: [4, 3] },
          })
        }
      }

      // 买卖点：文字标签（价格从 K 线数据里按日期查）
      const data = chart.getDataList()
      const rowByTs = new Map(data.map(d => [d.timestamp, d]))
      for (const m of markers) {
        const ts = new Date(m.date).getTime()
        const row = rowByTs.get(ts)
        if (!row) continue
        const price = m.above === false ? row.low : row.high
        const p = P(ts, price)
        if (!p) continue
        figs.push({
          type: 'text',
          attrs: {
            x: p.x,
            y: m.above === false ? p.y + 12 : p.y - 12,
            text: m.label,
            align: 'center',
            baseline: m.above === false ? 'top' : 'bottom',
          },
          styles: {
            color: m.kind === 'buy' ? BULL : m.kind === 'sell' ? BEAR : (m.color || '#FACC15'),
            size: 11,
            weight: 'bold',
          },
        })
      }

      return figs
    },
  })
}
