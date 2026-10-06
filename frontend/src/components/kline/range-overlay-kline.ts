/**
 * 区间高亮（ChartRange 原语）→ KLineChart overlay。
 *
 * 用于回测交易回放的「持仓区间」等横向日期区间色块；与缠论中枢(range)不同,
 * 这里不要求 ZG/ZD 两条水平线, 只画一个从主图顶到底的竖向半透明色块 + 左上角标签。
 */
import * as kc from 'klinecharts'
import type { ChartRange } from '@/lib/chart-primitives'

let registered = false

export function registerRangeOverlay() {
  if (registered) return
  registered = true

  kc.registerOverlay<ChartRange[]>({
    name: 'range',
    totalStep: 1,
    lock: true,
    needDefaultPointFigure: false,
    needDefaultXAxisFigure: false,
    needDefaultYAxisFigure: false,
    createPointFigures: ({ chart, overlay, bounding }) => {
      const ranges = overlay.extendData ?? []
      if (ranges.length === 0) return []
      const figs: kc.OverlayFigure[] = []
      const X = (date: string): number | null => {
        const c = chart.convertToPixel({ timestamp: new Date(date).getTime(), value: 0 }, { paneId: 'candle_pane' })
        if (Array.isArray(c) || c == null || c.x == null) return null
        return c.x
      }
      for (const r of ranges) {
        const x1 = X(r.start)
        const x2 = X(r.end)
        if (x1 == null || x2 == null) continue
        const x = Math.min(x1, x2)
        const w = Math.abs(x2 - x1)
        if (w <= 0) continue
        figs.push({
          type: 'rect',
          attrs: { x, y: bounding.top, width: w, height: Math.max(0, bounding.bottom - bounding.top) },
          styles: { style: 'fill', color: r.color || 'rgba(59,130,246,0.07)' },
        })
        if (r.label) {
          figs.push({
            type: 'text',
            attrs: { x: x + 3, y: bounding.top + 4, text: r.label, align: 'left', baseline: 'top' },
            styles: { color: '#60A5FA', size: 11 },
          })
        }
      }
      return figs
    },
  })
}
