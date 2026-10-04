/**
 * 事件时间轴 —— 图下方那条「事件索引条」。
 *
 * ★ 解决什么: P3 把信号 / 触发 / 买卖点都画上了 K 线, 但它们是全量平铺的。
 *   一只票两三年下来几十上百个标记, 缩略看时糊成一片; 想回到某天只能手拖滚动条
 *   找。这里把同样那批事件按日期排成一条, 点一下视口就跳过去(P4-2 的定位)。
 *
 * ★ 与图上标记的关系: 一一对应。条上显示的是**当前已启用的那几类事件**,
 *   绝不会出现「条上有、图上没有」—— 数据源是同一份, 只是呈现方式不同。
 *
 * ★ 横坐标按**bar 索引**而不是自然日: 休市/停牌在自然日轴上是空白, 而在 K 线上
 *   根本不存在, 按索引排才与图表 x 轴对齐。日期序列由渲染器经 chartBridge 上报。
 */
import { useMemo, useState } from 'react'
import { cn } from '@/lib/cn'
import { chartFocus } from '@/lib/chartBridge'
import {
  bucketTimeline,
  findDateIndex,
  TIMELINE_KIND_CN,
  type TimelineEvent,
  type TimelineKind,
} from '@/lib/chart-timeline'

const KIND_ORDER: TimelineKind[] = ['signal', 'alert', 'trade']
/**
 * 同一天有多类事件时条带取谁的颜色。
 * 告警排最前: 它是「发生了什么」, 比「该买该卖」更需要被一眼看见。
 */
const KIND_PRIORITY: Record<TimelineKind, number> = { alert: 0, trade: 1, signal: 2 }

export interface EventTimelineProps {
  /** 已合并好的事件(调用方按开关决定包含哪几类) */
  events: TimelineEvent[]
  /** 当前 K 线的日期序列(升序), 由渲染器上报 */
  dates: string[]
  className?: string
}

export function EventTimeline({ events, dates, className }: EventTimelineProps) {
  const [hidden, setHidden] = useState<TimelineKind[]>([])
  const [hover, setHover] = useState<number | null>(null)

  const counts = useMemo(() => {
    const m: Record<TimelineKind, number> = { signal: 0, alert: 0, trade: 0 }
    for (const e of events) m[e.kind] += 1
    return m
  }, [events])

  const buckets = useMemo(
    () => bucketTimeline(events.filter(e => !hidden.includes(e.kind))),
    [events, hidden],
  )

  const span = Math.max(1, dates.length - 1)
  const posOf = (date: string): number => {
    const i = findDateIndex(dates, date)
    return i < 0 ? 0 : (i / span) * 100
  }

  const toggleKind = (k: TimelineKind) => {
    setHidden(prev => (prev.includes(k) ? prev.filter(x => x !== k) : [...prev, k]))
  }

  const hovered = hover != null ? buckets[hover] : null

  return (
    <div className={cn('flex shrink-0 items-center gap-2 border-t border-border/60 px-2 py-1', className)}>
      <div className="flex shrink-0 items-center gap-1">
        {KIND_ORDER.map(k => (
          <button
            key={k}
            type="button"
            onClick={() => toggleKind(k)}
            disabled={counts[k] === 0}
            title={counts[k] === 0 ? `暂无${TIMELINE_KIND_CN[k]}事件` : `显示 / 隐藏${TIMELINE_KIND_CN[k]}事件`}
            className={cn(
              'h-5 rounded border px-1 text-[10px] font-mono transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent',
              counts[k] === 0
                ? 'cursor-not-allowed border-transparent text-muted/40'
                : hidden.includes(k)
                  ? 'border-transparent text-muted hover:bg-elevated'
                  : 'border-accent/30 bg-accent/20 text-accent',
            )}
          >
            {TIMELINE_KIND_CN[k]} {counts[k]}
          </button>
        ))}
      </div>

      <div className="relative h-5 min-w-0 flex-1">
        {/* 基线: 没有事件时也让这条轨道有意义 */}
        <div className="absolute left-0 right-0 top-1/2 h-px -translate-y-1/2 bg-border/70" />
        {dates.length === 0 && (
          <div className="absolute inset-0 grid place-items-center text-[10px] text-muted">等待 K 线数据</div>
        )}
        {buckets.length === 0 && dates.length > 0 && (
          <div className="absolute inset-0 grid place-items-center text-[10px] text-muted">
            {events.length === 0 ? '无事件, 打开上方「信号 / 触发 / 回测」开关' : '已全部隐藏'}
          </div>
        )}
        {buckets.map((b, i) => {
          const color = [...b.items].sort(
            (x, y) => KIND_PRIORITY[x.kind] - KIND_PRIORITY[y.kind],
          )[0].color
          return (
            <button
              key={`${b.date}-${i}`}
              type="button"
              onMouseEnter={() => setHover(i)}
              onMouseLeave={() => setHover(null)}
              onClick={() => chartFocus.request(b.date)}
              title={`${b.date} · ${b.items.map(it => it.label).join(' / ')}`}
              style={{ left: `calc(${posOf(b.date)}% - 1.5px)`, backgroundColor: color }}
              className="absolute top-0.5 h-4 w-[3px] rounded-sm opacity-80 transition-opacity hover:opacity-100 hover:w-[5px] focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent"
            />
          )
        })}
        {hovered && (
          <div
            className="pointer-events-none absolute bottom-full z-20 mb-1 max-w-[260px] -translate-x-1/2 rounded border border-border bg-elevated px-1.5 py-1 text-[10px] leading-4 text-foreground shadow-lg"
            style={{ left: `${Math.min(88, Math.max(12, posOf(hovered.date)))}%` }}
          >
            <div className="font-mono text-muted">{hovered.date}</div>
            {hovered.items.slice(0, 6).map((it, j) => (
              <div key={j} className="flex items-center gap-1 whitespace-nowrap">
                <span className="inline-block h-1.5 w-1.5 shrink-0 rounded-full" style={{ backgroundColor: it.color }} />
                <span className="text-[10px] text-muted">{TIMELINE_KIND_CN[it.kind]}</span>
                <span className="truncate">{it.label}</span>
              </div>
            ))}
            {hovered.items.length > 6 && (
              <div className="text-[10px] text-muted">…还有 {hovered.items.length - 6} 条</div>
            )}
          </div>
        )}
      </div>

      <div className="shrink-0 font-mono text-[10px] text-muted" title="点击条上的竖线, 视口会跳到那天">
        点击定位
      </div>
    </div>
  )
}
