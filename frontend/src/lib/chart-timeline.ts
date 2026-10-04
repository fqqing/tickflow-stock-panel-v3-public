/**
 * 事件时间轴的数据层 —— 把散在三处的事件(策略信号 / 监控触发 / 回测买卖点)
 * 收成一条可按日期定位、可按类型筛选的序列。
 *
 * ★ 为什么要有这一层: P3 已把三类事件都画到 K 线上了, 但它们是**全量平铺**的 ——
 *   一只票两三年下来几十上百个标记, 缩略看时糊成一片, 想回到某一天只能手拖
 *   滚动条找。时间轴给的是「导航」: 一眼看到事件分布, 点一下视口就过去。
 *
 * ★ 为什么不放进 chart-events.ts: 那边是「画在 K 线上的标记」(带上下方位),
 *   时间轴不需要方位, 但多了「类别」这个维度。混在一起两边都会被迫带上对方的
 *   字段。归约仍然共用同一份配色(见下面各 to* 函数), 不会漂移。
 *
 * 不依赖任何图表库: 两个内核与终端层都要消费, 这里必须是纯数据。
 */
import {
  collectSignalIds,
  SIGNAL_CN,
  SIGNAL_COLORS,
  signalKindOf,
} from '@/lib/signals'
import type { ChartEventPoint } from '@/lib/chart-events'

/** 事件来源类别 */
export type TimelineKind = 'signal' | 'alert' | 'trade'

export interface TimelineEvent {
  /** YYYY-MM-DD */
  date: string
  kind: TimelineKind
  /** 与 K 线标记同源的配色, 条带上的点与图上的三角是同一个颜色 */
  color: string
  label: string
}

export const TIMELINE_KIND_CN: Record<TimelineKind, string> = {
  signal: '信号',
  alert: '触发',
  trade: '回测',
}

const byDate = (a: TimelineEvent, b: TimelineEvent): number => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0)

/**
 * 一行日K → 信号事件。行里带 signal_* 布尔列(只有日线档有, 见 klineChartFields)。
 *
 * 入参故意写成 unknown[]: KlineRow / OHLC 这类具体类型没有索引签名, 直接按
 * Record<string, unknown> 收会要求调用方逐个 cast。这里内部收窄一次就够。
 */
export function signalRowsToTimeline(rows: readonly unknown[] | null | undefined): TimelineEvent[] {
  const out: TimelineEvent[] = []
  for (const raw0 of rows ?? []) {
    const r = raw0 as Record<string, unknown> | null | undefined
    if (!r) continue
    const raw = r.date
    if (typeof raw !== 'string' || raw.length < 10) continue
    const date = raw.slice(0, 10)
    for (const id of collectSignalIds(r)) {
      const kind = signalKindOf(id)
      out.push({ date, kind: 'signal', color: SIGNAL_COLORS[kind], label: SIGNAL_CN.get(id) ?? id })
    }
  }
  return out.sort(byDate)
}

/** K 线标记 → 时间轴事件(告警 / 买卖点各调一次, kind 由调用方指定) */
export function eventPointsToTimeline(points: readonly ChartEventPoint[] | null | undefined, kind: TimelineKind): TimelineEvent[] {
  const out: TimelineEvent[] = []
  for (const p of points ?? []) {
    if (!p?.date) continue
    for (const m of p.marks) {
      out.push({ date: p.date, kind, color: m.color, label: m.label })
    }
  }
  return out.sort(byDate)
}

export function mergeTimeline(...groups: (TimelineEvent[] | null | undefined)[]): TimelineEvent[] {
  const out: TimelineEvent[] = []
  for (const g of groups) {
    for (const e of g ?? []) out.push(e)
  }
  return out.sort(byDate)
}

/** 同一天的事件合成一格: 条带上一天只占一个位置, 悬浮时再看明细 */
export interface TimelineBucket {
  date: string
  items: TimelineEvent[]
}

export function bucketTimeline(list: readonly TimelineEvent[]): TimelineBucket[] {
  const byDateMap = new Map<string, TimelineEvent[]>()
  for (const e of list) {
    const arr = byDateMap.get(e.date)
    if (arr) arr.push(e)
    else byDateMap.set(e.date, [e])
  }
  return [...byDateMap.entries()]
    .map(([date, items]) => ({ date, items }))
    .sort((a, b) => (a.date < b.date ? -1 : 1))
}

/**
 * 在升序日期序列里定位目标日。
 *
 * 周/月线的 bar 日期是周末/月末, 而事件日期是自然交易日 —— 精确匹配不上是常态,
 * 所以退到「最后一个不晚于目标的 bar」, 也就是事件发生时图上真实存在的那一根。
 * 目标早于全部数据时落第一根(而不是返回 -1): 那时跳过去总比没反应好。
 */
export function findDateIndex(dates: readonly string[], target: string): number {
  const n = dates.length
  if (n === 0) return -1
  let lo = 0
  let hi = n - 1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (dates[mid] === target) return mid
    if (dates[mid] < target) lo = mid + 1
    else hi = mid - 1
  }
  return lo > 0 ? lo - 1 : 0
}
