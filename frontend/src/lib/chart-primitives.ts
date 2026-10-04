/**
 * 图表原语 —— 与具体图表库无关的中间表示(IR)。
 *
 * 为什么单独抽出来:
 * 这四个类型原先定义在 `components/EChartsCandlestick.tsx` 里, 而
 * `lib/chan-overlay.ts`(缠论)、`components/StockDailyKChart.tsx`、`pages/` 多处都从
 * 那个 1718 行的巨石组件 import 它们。也就是说 —— 换图表内核时, 光是「把类型
 * 搬走」就会牵动全项目。
 *
 * 抽到这里之后, 渲染层变成可替换的:
 *   缠论/监控/画线 → 产出本文件的原语 → 由具体图表库适配成自己的绘制指令
 * ECharts 有 ECharts 的适配器, 换成 KLineChart 就写 KLineChart 的适配器,
 * 上游(缠论等)一行都不用改。
 */
export interface ChartMarker {
  date: string
  kind: 'buy' | 'sell' | 'neutral'
  label?: string
  /** 若为 true，标记放在蜡烛上方（如涨停连板标签）。 */
  above?: boolean
  /** 自定义标签颜色，覆盖默认的 kind 对应色。 */
  color?: string
}

export interface ChartPolyline {
  points: { date: string; price: number }[]
  color?: string
  /** 线宽，默认 1.2 */
  width?: number
  dashed?: boolean
  name?: string
  /** 在顶点处画小圆点，默认 false */
  showSymbol?: boolean
}

export interface ChartPriceLine {
  value: number
  label?: string
  color?: string
  start?: string
  end?: string
}

export interface ChartRange {
  start: string
  end: string
  label?: string
  color?: string
}
