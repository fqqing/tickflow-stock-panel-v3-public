/**
 * 用户手绘趋势线 → KLineChart overlay。
 *
 * 端点是 (date, price), 由画线交互层在 mouseup 时 snap 到最近交易日 + 像素价格后写入。
 * 画法: 每条线两个端点的实线, 颜色固定琥珀色(与旧 ECharts 侧 DRAW_COLOR 一致)。
 * 只画主图(candle_pane); 端点 date 是日线日期, 周/月轴找不到对应位置时自然跳过。
 */
import * as kc from 'klinecharts'

export interface DrawLinePoint {
  date: string
  price: number
}

export interface DrawLine {
  a: DrawLinePoint
  b: DrawLinePoint
}

export const DRAW_COLOR = '#F59E0B'

let registered = false

export function registerDrawLineOverlay(): void {
  if (registered) return
  registered = true

  kc.registerOverlay<DrawLine[]>({
    name: 'drawLine',
    totalStep: 1,
    lock: true,
    needDefaultPointFigure: false,
    needDefaultXAxisFigure: false,
    needDefaultYAxisFigure: false,
    createPointFigures: ({ chart, overlay }) => {
      const lines = overlay.extendData ?? []
      if (lines.length === 0) return []
      const figs: kc.OverlayFigure[] = []
      const P = (date: string, price: number): kc.Coordinate | null => {
        const c = chart.convertToPixel({ timestamp: new Date(date).getTime(), value: price }, { paneId: 'candle_pane' })
        if (Array.isArray(c) || c == null || c.x == null || c.y == null) return null
        return { x: c.x, y: c.y }
      }
      for (const l of lines) {
        const pa = P(l.a.date, l.a.price)
        const pb = P(l.b.date, l.b.price)
        if (!pa || !pb) continue
        figs.push({
          type: 'line',
          attrs: { coordinates: [{ x: pa.x, y: pa.y }, { x: pb.x, y: pb.y }] },
          styles: { color: DRAW_COLOR, size: 1.5, style: 'solid' },
        })
      }
      return figs
    },
  })
}
