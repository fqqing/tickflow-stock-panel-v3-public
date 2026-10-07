/**
 * 蛟龙出海信号标记 → KLineChart overlay。
 *
 * 与 signal-markers / limit-up-markers 同一套管线: 数据由 KLinePro 在 rowToKLine
 * 挂到 KLineData.td_signal 上, overlay 从 getDataList() 读, 只在日线档画。
 *
 * 之所以不用 DRAGON 指标的 figure 画信号点: klinecharts 10.0.3 指标渲染器对
 * 「条件性显示」的 figure(稀疏 key, 或 dense key + attrs 按值条件)存在坐标管道
 * 问题 —— 实测要么不渲染, 要么错位到可视区第一根 bar; 而 dense 的 line 正常。
 * overlay 的 createPointFigures 自己遍历 getDataList(), 无此问题。
 *
 * 画法: 黄色下三角(尖端朝下指向前一日低点方向), 画在信号日 LOW 下方。
 * bar 间距过小时(< 6px)省略, 避免糊成一片。性能: 只画可视区间内的 bar。
 */
import * as kc from 'klinecharts'

/** 蛟龙出海信号标记用色(与 ECharts 侧 DRAGON_SIGNAL_COLOR 一致) */
const DRAGON_SIGNAL = '#FACC15'

export function registerDragonMarkersOverlay(): void {
  kc.registerOverlay({
    name: 'dragonMarkers',
    totalStep: 1,
    lock: true,
    needDefaultPointFigure: false,
    needDefaultXAxisFigure: false,
    needDefaultYAxisFigure: false,
    createPointFigures: ({ chart, xAxis, bounding }) => {
      const data = chart.getDataList()
      if (data.length === 0) return []

      // ── 裁到可视区间 ──
      let from = 0
      let to = data.length - 1
      if (xAxis) {
        // bounding.left/right 是 y 轴宽度(inset)而非 pane-local 像素
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

      // bar 间距: 太小时三角会互相粘连
      let barW = 6
      if (to > from) {
        const a = P(data[from].timestamp, Number(data[from].close))
        const b = P(data[from + 1].timestamp, Number(data[from + 1].close))
        if (a && b) barW = Math.abs(b.x - a.x)
      }

      const figs: kc.OverlayFigure[] = []
      for (let i = from; i <= to; i += 1) {
        const d = data[i]
        if (!d.td_signal) continue
        const p = P(d.timestamp, Number(d.low))
        if (!p) continue
        const s = barW >= 10 ? 4.5 : 3
        const y = p.y + 8
        // 尖端朝下的三角(指向信号日 LOW 下方), 与 ECharts 侧 arrow 同方向
        figs.push({
          type: 'polygon',
          attrs: {
            coordinates: [
              { x: p.x - s, y },
              { x: p.x + s, y },
              { x: p.x, y: y + s * 1.6 },
            ],
          },
          styles: { style: 'fill', color: DRAGON_SIGNAL },
        })
      }
      return figs
    },
  })
}
