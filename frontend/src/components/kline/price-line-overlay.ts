/**
 * 监控价位线（ChartPriceLine 原语）→ KLineChart overlay。
 *
 * 用单 overlay 画所有水平线 + 右侧标签；不会撑开 y 轴范围。
 */
import * as kc from 'klinecharts'
import type { ChartPriceLine } from '@/lib/chart-primitives'

let registered = false

export function registerPriceLineOverlay() {
  if (registered) return
  registered = true

  kc.registerOverlay<ChartPriceLine[]>({
    name: 'priceLine',
    totalStep: 1,
    lock: true,
    needDefaultPointFigure: false,
    needDefaultXAxisFigure: false,
    needDefaultYAxisFigure: false,
    createPointFigures: ({ chart, overlay, bounding }) => {
      const lines = overlay.extendData ?? []
      if (lines.length === 0) return []
      const data = chart.getDataList()
      if (data.length === 0) return []
      const first = data[0].timestamp
      const last = data[data.length - 1].timestamp
      const figs: kc.OverlayFigure[] = []
      const P = (ts: number, v: number): kc.Coordinate | null => {
        const c = chart.convertToPixel({ timestamp: ts, value: v }, { paneId: 'candle_pane' })
        if (Array.isArray(c) || c == null || c.x == null || c.y == null) return null
        return { x: c.x, y: c.y }
      }
      for (const pl of lines) {
        const pa = P(first, pl.value)
        const pb = P(last, pl.value)
        if (!pa || !pb) continue
        figs.push({
          type: 'line',
          attrs: { coordinates: [{ x: bounding.left, y: pa.y }, { x: bounding.right, y: pb.y }] },
          styles: { color: pl.color || '#F79009', size: 1.5, style: 'solid' },
        })
        if (pl.label) {
          figs.push({
            type: 'text',
            attrs: { x: bounding.right - 4, y: pa.y - 6, text: pl.label, align: 'right', baseline: 'bottom' },
            styles: { color: pl.color || '#F79009', size: 11 },
          })
        }
      }
      return figs
    },
  })
}
