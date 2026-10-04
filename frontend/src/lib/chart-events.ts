/**
 * K 线叠加层的「外部事件」数据归约 —— 监控触发 / 回测买卖点共用一套结构。
 *
 * 为什么要有这一层:
 *   两个数据源的后端结构完全不同 (一个是按天聚合的触发点, 一个是按笔的交易
 *   记录), 但画到图上都只需要「某天 + 画什么颜色 + 写什么字 + 画在上面还是
 *   下面」。各自直接在组件里归约, 两个内核很快会漂移成两套观感 —— 这正是
 *   之前切 K 线内核觉得割裂的原因。所以在这里统一成 ChartEventPoint, 两个
 *   内核都消费它。
 *
 * 不依赖 klinecharts: ECharts 侧也要用, 保持纯数据。
 */

/** 一个标记: 画在 K 线上方(above)还是下方(below) */
export interface ChartEventMark {
  label: string
  color: string
  side: 'above' | 'below'
}

/** 某一天要画的所有标记 */
export interface ChartEventPoint {
  /** YYYY-MM-DD, 与 K 线的 date 同源 (日K) */
  date: string
  marks: ChartEventMark[]
}

// ── 配色 ────────────────────────────────────────────────────
// 买卖沿用 signal-markers 的红买/绿卖 (A 股红涨绿跌), 避免同一张图上两套
// 买卖配色互相打架。告警用琥珀/红区分严重程度, 与买卖色系错开。
export const TRADE_BUY_COLOR = '#F04438'
export const TRADE_SELL_COLOR = '#12B76A'
export const ALERT_CRITICAL_COLOR = '#B42318'
export const ALERT_WARN_COLOR = '#F79009'
export const ALERT_INFO_COLOR = '#3B82F6'

/** 告警严重程度 → 颜色。未知等级按 info 兜底。 */
export function alertColor(severity: string | undefined): string {
  if (severity === 'critical') return ALERT_CRITICAL_COLOR
  if (severity === 'warn') return ALERT_WARN_COLOR
  return ALERT_INFO_COLOR
}

// ── 归约: 监控触发 ───────────────────────────────────────────

export interface AlertTriggerPoint {
  date: string
  ts: number
  count: number
  severity: string
  labels: string[]
  signals: string[]
}

/**
 * 触发点 → 图标记。
 *
 * 告警一律画在 K 线**上方**: 它们是「发生了什么事」而不是「该买该卖」, 放在
 * 上方不会和买入三角(下方)抢位置。同一天多条合并成一个标记, 标签取第一条,
 * 条数大于 1 时补 xN —— 后端已经按天聚合过, 这里的合并是防重入。
 */
export function alertPointsToEvents(points: AlertTriggerPoint[]): ChartEventPoint[] {
  const byDate = new Map<string, ChartEventPoint>()
  for (const p of points) {
    if (!p?.date) continue
    const label = p.labels?.[0] || p.signals?.[0] || '触发'
    const mark: ChartEventMark = {
      label: p.count > 1 ? `${label} x${p.count}` : label,
      color: alertColor(p.severity),
      side: 'above',
    }
    const existing = byDate.get(p.date)
    if (existing) existing.marks.push(mark)
    else byDate.set(p.date, { date: p.date, marks: [mark] })
  }
  return [...byDate.values()].sort((a, b) => (a.date < b.date ? -1 : 1))
}

// ── 归约: 回测买卖点 ──────────────────────────────────────────

export interface BacktestTradeMark {
  symbol: string
  entry_date: string
  exit_date: string
  entry_price?: number | null
  exit_price?: number | null
  pnl_pct?: number | null
  exit_reason?: string | null
}

const pct = (v: number | null | undefined): string => {
  if (v == null || !Number.isFinite(v)) return ''
  return `${v >= 0 ? '+' : ''}${(v * 100).toFixed(1)}%`
}

/**
 * 交易记录 → 图标记。一笔交易拆成两个点: 买在下方、卖在上方。
 *
 * 卖点带上盈亏百分比 (亏的也带, 复盘时「这笔为什么亏」比「赚了多少」更需要
 * 被看见)。同一天可能有多笔(多标的回测时同日开仓), 按 marks 堆叠。
 */
export function tradesToEvents(trades: BacktestTradeMark[]): ChartEventPoint[] {
  const byDate = new Map<string, ChartEventPoint>()
  const push = (date: string, mark: ChartEventMark) => {
    if (!date) return
    const existing = byDate.get(date)
    if (existing) existing.marks.push(mark)
    else byDate.set(date, { date, marks: [mark] })
  }
  for (const t of trades) {
    if (!t) continue
    push(t.entry_date.slice(0, 10), {
      label: '买',
      color: TRADE_BUY_COLOR,
      side: 'below',
    })
    const pnl = pct(t.pnl_pct)
    push(t.exit_date.slice(0, 10), {
      label: pnl ? `卖 ${pnl}` : '卖',
      color: TRADE_SELL_COLOR,
      side: 'above',
    })
  }
  return [...byDate.values()].sort((a, b) => (a.date < b.date ? -1 : 1))
}

/** 合并多个来源 (先告警后买卖, 同日叠加) */
export function mergeEventPoints(...groups: ChartEventPoint[][]): ChartEventPoint[] {
  const byDate = new Map<string, ChartEventPoint>()
  for (const g of groups) {
    for (const p of g) {
      const existing = byDate.get(p.date)
      if (existing) existing.marks.push(...p.marks)
      else byDate.set(p.date, { date: p.date, marks: [...p.marks] })
    }
  }
  return [...byDate.values()].sort((a, b) => (a.date < b.date ? -1 : 1))
}
