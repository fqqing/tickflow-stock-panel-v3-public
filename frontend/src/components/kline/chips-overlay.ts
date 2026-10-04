/**
 * 筹码分布(成本分布) → KLineChart overlay。
 *
 * 画法: 在 K 线窗格**右侧**画横向筹码条, 每档一条矩形, 长度按该档筹码占比;
 * 另画一条平均成本虚线 + 标签。
 *
 * 为什么画在右侧而不是每根 K 线上:
 *   「每日筹码演化」要 N 天 x M 档个矩形, 上千个 figure 会拖垮渲染; 而且用户
 *   真正要看的是**当前**成本结构。所以只在右侧画一份当前分布。
 */
import * as kc from 'klinecharts'

export interface ChipsBin {
  price: number
  ratio: number
}

export interface ChipsOverlayData {
  bins: ChipsBin[]
  /** 当前价: 低于它的是获利盘(红), 高于的是套牢盘(绿) */
  close: number | null
  avg_cost: number | null
  /** 价格档宽度(用于算条高), 无则按像素均分 */
  step?: number
}

/** 筹码条占窗格宽度的比例 */
const WIDTH_RATIO = 0.34
const PROFIT = 'rgba(239, 68, 68, 0.55)'   // 获利盘(红)
const LOCKED = 'rgba(16, 185, 129, 0.45)'  // 套牢盘(绿)
const AVG_LINE = '#F79009'

let registered = false

export function registerChipsOverlay() {
  if (registered) return
  registered = true

  kc.registerOverlay<ChipsOverlayData>({
    name: 'chips',
    totalStep: 1,
    lock: true,
    needDefaultPointFigure: false,
    needDefaultXAxisFigure: false,
    needDefaultYAxisFigure: false,
    createPointFigures: ({ chart, overlay, bounding }) => {
      const d = overlay.extendData
      if (!d || !d.bins || d.bins.length === 0) return []
      const data = chart.getDataList()
      if (data.length === 0) return []
      const lastTs = data[data.length - 1].timestamp
      const firstTs = data[0].timestamp

      const P = (value: number): number | null => {
        const c = chart.convertToPixel({ timestamp: lastTs, value }, { paneId: 'candle_pane' })
        if (Array.isArray(c) || c == null || c.y == null) return null
        return c.y
      }

      const maxRatio = Math.max(...d.bins.map(b => b.ratio))
      if (!(maxRatio > 0)) return []
      const barW = (bounding.right - bounding.left) * WIDTH_RATIO
      const figs: kc.OverlayFigure[] = []

      // 条高: 优先用真实价格档宽换算的像素高, 退化时按档数均分
      const ys = d.bins.map(b => P(b.price))
      let barH = 0
      if (d.step && d.step > 0) {
        const y0 = P(d.bins[0].price)
        const y1 = P(d.bins[0].price + d.step)
        if (y0 != null && y1 != null) barH = Math.abs(y1 - y0)
      }
      if (!(barH > 0)) {
        const valid = ys.filter((y): y is number => y != null)
        if (valid.length >= 2) {
          barH = Math.abs(valid[valid.length - 1] - valid[0]) / (valid.length - 1)
        }
      }
      barH = Math.max(1.5, Math.min(barH || 2, 14))

      d.bins.forEach((b, i) => {
        const y = ys[i]
        if (y == null || b.ratio <= 0) return
        const w = (b.ratio / maxRatio) * barW
        const color = d.close != null && b.price < d.close ? PROFIT : LOCKED
        figs.push({
          type: 'rect',
          attrs: { x: bounding.right - w, y: y - barH / 2, width: w, height: barH },
          styles: { style: 'fill', color },
        })
      })

      // 平均成本线 + 标签
      if (d.avg_cost != null) {
        const y = P(d.avg_cost)
        if (y != null) {
          const left = chart.convertToPixel(
            { timestamp: firstTs, value: d.avg_cost }, { paneId: 'candle_pane' },
          )
          const x0 = Array.isArray(left) || left == null || left.x == null ? bounding.left : left.x
          figs.push({
            type: 'line',
            attrs: { coordinates: [{ x: x0, y }, { x: bounding.right, y }] },
            styles: { color: AVG_LINE, size: 1, style: 'dashed', dashedValue: [4, 3] },
          })
          figs.push({
            type: 'text',
            attrs: {
              x: bounding.right - 4, y: y - 4, text: `平均成本 ${d.avg_cost.toFixed(2)}`,
              align: 'right', baseline: 'bottom',
            },
            styles: { color: AVG_LINE, size: 11 },
          })
        }
      }
      return figs
    },
  })
}
