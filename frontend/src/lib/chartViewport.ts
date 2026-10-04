/**
 * 视口(缩放 + 滚动位置)的跨内核互译。
 *
 * ★ 统一表达: { visibleBars, offsetRight } —— 可见多少根 + 右侧还剩几根没显示。
 *   之所以不用像素或日期区间, 是因为两个内核的缩放语义根本不同:
 *     - klinecharts: 按 barSpace(每根占多少像素)缩放, 滚动用像素距离
 *     - ECharts:     dataZoom 的 start/end 是**百分比**
 *   唯有「多少根 + 右边留几根」两边都能无损互译, 切换后视口才不漂移。
 *
 * ★ 精度: 两侧换算都是取整的近似(尤其 klinecharts 的 barSpace 受最小/最大
 *   间距钳制)。目标是「切换后落在同一区间」, 不承诺逐像素一致。
 */
import type { Chart } from 'klinecharts'
import type { ChartViewport } from '@/lib/chartSession'

export interface EChartsZoom {
  start: number
  end: number
}

const clampPct = (v: number): number => Math.max(0, Math.min(100, v))

/** dataZoom 百分比 -> 视口。total 为当前 K 线总根数 */
export function zoomToViewport(zoom: EChartsZoom, total: number): ChartViewport | null {
  if (!(total > 0)) return null
  const start = clampPct(zoom.start)
  const end = clampPct(zoom.end)
  const visibleBars = Math.max(1, Math.round((total * (end - start)) / 100))
  const offsetRight = Math.max(0, Math.round((total * (100 - end)) / 100))
  return { visibleBars, offsetRight }
}

/** 视口 -> dataZoom 百分比 */
export function viewportToZoom(vp: ChartViewport, total: number): EChartsZoom {
  if (!(total > 0)) return { start: 0, end: 100 }
  const visibleBars = Math.max(1, Math.min(Math.round(vp.visibleBars), total))
  const offsetRight = Math.max(0, Math.min(Math.round(vp.offsetRight), Math.max(0, total - visibleBars)))
  const end = 100 - (offsetRight / total) * 100
  const start = end - (visibleBars / total) * 100
  return { start: clampPct(start), end: clampPct(end) }
}

/** 单根 K 占的像素宽(柱体 + 间隙) */
function barStep(chart: Chart): number {
  const bs = chart.getBarSpace()
  const step = bs.bar + bs.gapBar
  return step > 0 ? step : 1
}

/** klinecharts: 读当前视口 */
export function readKcViewport(chart: Chart): ChartViewport | null {
  const width = chart.getSize('candle_pane', 'main')?.width ?? 0
  if (!(width > 0)) return null
  const step = barStep(chart)
  return {
    visibleBars: Math.max(1, Math.round(width / step)),
    offsetRight: Math.max(0, Math.round(chart.getOffsetRightDistance() / step)),
  }
}

const MIN_VISIBLE = 10
const MAX_VISIBLE = 2000

/** klinecharts: 应用视口(先定缩放, 再定右侧留白) */
export function applyKcViewport(chart: Chart, vp: ChartViewport): void {
  const width = chart.getSize('candle_pane', 'main')?.width ?? 0
  if (!(width > 0)) return
  const visibleBars = Math.max(MIN_VISIBLE, Math.min(Math.round(vp.visibleBars), MAX_VISIBLE))
  chart.setBarSpace(width / visibleBars)
  // setBarSpace 之后 barSpace 才更新, 右侧留白要按新的步长折算
  chart.setOffsetRightDistance(Math.max(0, vp.offsetRight) * barStep(chart))
}
