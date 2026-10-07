/**
 * 量能对比 → KLineChart overlay。
 *
 * 在成交量副图柱顶显示「量比」标签(当前量 / 前 N 个交易日均量), 与旧 ECharts 侧
 * volumeCompare 同口径。成交量值已在 KLineData.volume 上, 前端只算一个滑窗均量,
 * 不再重算成交量本身。
 *
 * extendData 传 { days } 控制分母窗口(前 N 日, 不含当日)。文字色跟随主题, 避免
 * 明暗主题切换后标签糊掉。标签只在 bar 间距足够时画, 缩得太密时省略。
 */
import * as kc from 'klinecharts'

export interface VolumeComparePayload {
  /** 前 N 日均量(量比分母窗口, 1~20) */
  days: number
  /** 数据版本号(仅作重绘信号, overlay 不读它), 由 KLinePro 注入 structRev */
  rev?: string
}

/** 画文字所需的最小 bar 间距(px), 与 signal/limit-up 标记对齐 */
const TEXT_MIN_BAR_W = 10

let registered = false

export function registerVolumeCompareOverlay(): void {
  if (registered) return
  registered = true

  kc.registerOverlay<VolumeComparePayload>({
    name: 'volumeCompare',
    totalStep: 1,
    lock: true,
    needDefaultPointFigure: false,
    needDefaultXAxisFigure: false,
    needDefaultYAxisFigure: false,
    createPointFigures: ({ chart, overlay, xAxis, bounding }) => {
      const days = Math.max(1, Math.min(20, Math.round(overlay.extendData?.days ?? 1)))
      const data = chart.getDataList()
      if (data.length === 0) return []

      // ── 裁到可视区间 ──
      let from = 0
      let to = data.length - 1
      if (xAxis) {
        // bounding.left/right 是 y 轴宽度(inset)而非 pane-local 像素，xAxis 像素空间是 [0, bounding.width]
        const left = xAxis.convertTimestampFromPixel(0)
        const right = xAxis.convertTimestampFromPixel(bounding.width)
        if (left != null && Number.isFinite(left)) {
          while (from < to && data[from].timestamp < left) from += 1
          from = Math.max(0, from - 1)
        }
        if (right != null && Number.isFinite(right)) {
          while (to > from && data[to].timestamp > right) to -= 1
          to = Math.min(data.length - 1, to + 1)
        }
      }

      // bar 间距决定文字要不要画
      const X = (ts: number): number | null => {
        const c = chart.convertToPixel({ timestamp: ts, value: 0 }, { paneId: overlay.paneId })
        if (Array.isArray(c) || c == null || c.x == null) return null
        return c.x
      }
      let barW = 6
      if (to > from) {
        const a = X(data[from].timestamp)
        const b = X(data[from + 1].timestamp)
        if (a != null && b != null) barW = Math.abs(b - a)
      }
      if (barW < TEXT_MIN_BAR_W) return []

      // 文字色跟随主题(x 轴刻度文字色即「普通文字」色)
      const styles = chart.getStyles()
      const textColor = styles?.xAxis?.tickText?.color ?? '#64748B'

      const figs: kc.OverlayFigure[] = []
      for (let i = from; i <= to; i += 1) {
        const d = data[i]
        const vol = Number(d.volume ?? 0)
        if (!(vol > 0) || i < days) continue
        let sum = 0
        for (let j = i - days; j < i; j += 1) sum += Number(data[j]?.volume ?? 0)
        if (sum <= 0) continue
        const ratio = vol / (sum / days)
        const c = chart.convertToPixel({ timestamp: d.timestamp, value: vol }, { paneId: overlay.paneId })
        if (Array.isArray(c) || c == null || c.x == null || c.y == null) continue
        figs.push({
          type: 'text',
          attrs: {
            x: c.x,
            y: c.y - 2,
            text: `${ratio.toFixed(1)}x`,
            align: 'center',
            baseline: 'bottom',
          },
          styles: { color: textColor, size: 8 },
        })
      }
      return figs
    },
  })
}
