/**
 * 策略信号标记 → KLineChart overlay。
 *
 * 数据来自 enriched 表的 signal_* 布尔列, 由 KLinePro 在 rowToKLine 里收集成
 * id 列表挂到 KLineData.sig 上(见 collectSignals), 前端不重算任何信号 ——
 * 与选股页/回测页用的是同一份后端结果, 不会出现「图上标了但选股没选出」。
 *
 * 画法: 买入类(kind=entry)三角画在 LOW 下方, 卖出类(exit)画在 HIGH 上方,
 * 双向类(both, 如放量)画在下方用蓝色区分。同一根 K 线触发多个信号时按纵向
 * 槽位堆叠。中文名来自 signals.ts, 与后端 monitor.py 的 _SIGNAL_CN 对齐。
 *
 * 文字只在 bar 间距 >= 12px 时画: K 线缩得很密时文字会糊成一片, 那时只留三角。
 * 性能: 与 structure-overlay 一样只画可视区间内的 bar。
 */
import * as kc from 'klinecharts'
import {
  collectSignalIds,
  SIGNAL_CN,
  SIGNAL_COLORS,
  signalKindOf,
} from '@/lib/signals'

/**
 * 中文名 / 配色 / 取信号 id 的实现都已下沉到 lib/signals —— 事件时间轴也要用,
 * 而它不能 import 本文件(会拉进 klinecharts)。这里 re-export 保持既有 import
 * 路径可用, 三个消费方拿到的是同一份定义。
 */
export { collectSignalIds, SIGNAL_CN, SIGNAL_COLORS, signalKindOf }

/** 一根 K 线最多画几个信号: 再多堆不下, 可读性也反而更差 */
const MAX_PER_BAR = 4
/** 纵向槽位间距(px) */
const SLOT = 13
/** 画文字所需的最小 bar 间距(px) */
const TEXT_MIN_BAR_W = 12

export interface SignalMarkersPayload {
  /**
   * 数据版本号。overlay 不靠 extendData 取数(数从 getDataList 读), 但需要一个
   * 变化信号触发 createPointFigures 重跑 —— rows/周期变化时 KLinePro 会改写它。
   */
  rev: string
}

export function registerSignalMarkersOverlay(): void {
  kc.registerOverlay({
    name: 'signalMarkers',
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
        const left = xAxis.convertTimestampFromPixel(bounding.left)
        const right = xAxis.convertTimestampFromPixel(bounding.right)
        if (left != null && Number.isFinite(left)) {
          while (from < to && data[from].timestamp < left) from += 1
          from = Math.max(0, from - 1) // 多留一根, 避免左边缘缺口
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

      // bar 间距决定文字要不要画
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
        const ids = d.sig as unknown as string[] | undefined
        if (!Array.isArray(ids) || ids.length === 0) continue
        const lowP = P(d.timestamp, Number(d.low))
        const highP = P(d.timestamp, Number(d.high))
        if (!lowP || !highP) continue

        let below = 0
        let above = 0
        for (const id of ids.slice(0, MAX_PER_BAR)) {
          const kind = signalKindOf(id)
          const isExit = kind === 'exit'
          const color = SIGNAL_COLORS[kind]
          const slot = isExit ? above++ : below++
          const p = isExit ? highP : lowP
          const y = isExit ? p.y - 6 - slot * SLOT : p.y + 6 + slot * SLOT
          const s = 4.5
          const tipY = isExit ? y - s : y + s
          figs.push({
            type: 'polygon',
            attrs: {
              coordinates: [
                { x: p.x, y: tipY },
                { x: p.x - s, y },
                { x: p.x + s, y },
              ],
            },
            styles: { style: 'fill', color },
          })
          if (showText) {
            figs.push({
              type: 'text',
              attrs: {
                x: p.x,
                y: isExit ? tipY - 2 : tipY + 2,
                text: SIGNAL_CN.get(id) ?? id,
                align: 'center',
                baseline: isExit ? 'bottom' : 'top',
              },
              styles: { color, size: 10 },
            })
          }
        }
      }
      return figs
    },
  })
}
