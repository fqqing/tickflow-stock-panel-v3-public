/**
 * 涨停 / 连板 / 炸板标记 → KLineChart overlay。
 *
 * 数据来自 enriched 表的 signal_limit_up / signal_broken_limit_up 布尔列 +
 * consecutive_limit_ups 数字列, 由 KLinePro 在 rowToKLine 里归约成 limitUp
 * 挂到 KLineData 上(前端不重算), 与选股页/回测页同源。
 *
 * 画法与旧 ECharts 侧 buildLimitUpMarkers 对齐:
 *   - 涨停(封板)  金色 #FACC15, 1 板标「板」, N 连板标数字 N
 *   - 炸板         紫色 #8B5CF6, 标「炸」
 * 都画在 K 线 HIGH 上方; bar 间距足够时画文字, 缩得太密时退化成小圆点。
 */
import * as kc from 'klinecharts'

const BOARD_COLOR = '#FACC15'
const BREAK_COLOR = '#8B5CF6'
/** 画文字所需的最小 bar 间距(px) */
const TEXT_MIN_BAR_W = 12

export interface LimitUpData {
  kind: 'board' | 'break'
  boards: number
}

let registered = false

export function registerLimitUpMarkersOverlay(): void {
  if (registered) return
  registered = true

  kc.registerOverlay({
    name: 'limitUpMarkers',
    totalStep: 1,
    lock: true,
    needDefaultPointFigure: false,
    needDefaultXAxisFigure: false,
    needDefaultYAxisFigure: false,
    createPointFigures: ({ chart, xAxis, bounding }) => {
      const data = chart.getDataList()
      if (data.length === 0) return []

      // ── 裁到可视区间(与 signal-markers 同款) ──
      let from = 0
      let to = data.length - 1
      if (xAxis) {
        // ★ bounding 是 pane main widget 的 bounding：left/right 是左右 y 轴宽度(inset)，
        //   不是 pane-local 像素。xAxis 的像素空间是 [0, bounding.width]，误用 bounding.left/right
        //   会把右边界算成「左起 ~几十 px」，导致 to 塌缩到最左、右端(涨停往往在最右)整片漏画。
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
        const lu = (d as { limitUp?: LimitUpData | null }).limitUp
        if (!lu) continue
        const highP = P(d.timestamp, Number(d.high))
        if (!highP) continue

        const isBreak = lu.kind === 'break'
        const label = isBreak ? '炸' : lu.boards <= 1 ? '板' : String(lu.boards)
        const color = isBreak ? BREAK_COLOR : BOARD_COLOR

        if (showText) {
          figs.push({
            type: 'text',
            attrs: {
              x: highP.x,
              y: highP.y - 6,
              text: label,
              align: 'center',
              baseline: 'bottom',
            },
            styles: {
              color,
              size: 10,
              weight: 'bold',
              // 关掉 klinecharts 默认蓝底(text 默认 backgroundColor: BLUE)
              backgroundColor: 'transparent',
              paddingLeft: 0,
              paddingTop: 0,
              paddingRight: 0,
              paddingBottom: 0,
            },
          })
        } else {
          figs.push({
            type: 'circle',
            attrs: { x: highP.x, y: highP.y - 4, r: 2 },
            styles: { style: 'fill', color },
          })
        }
      }
      return figs
    },
  })
}
