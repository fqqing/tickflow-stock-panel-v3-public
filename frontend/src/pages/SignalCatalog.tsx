/**
 * 信号函数库（Signal Catalog）。
 *
 * 展示「信号函数统一层」（借鉴 czsc 信号-事件-交易体系）的可复用原子信号清单：
 * 每个信号是一个可注册、可列出、可组合的布尔函数，组合表达式在声明式策略
 * （signal_combo 后端）里用 all_of / any_of / not_of 引用。
 *
 * 与「信号实验室」的分工：信号实验室复盘「信号后来怎么走」；这里回答「有哪些
 * 原子信号可用、各自方向 / 依赖字段 / 参数 / 开关」——是组合表达式的零件目录。
 */
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Library, RefreshCw, Search, SlidersHorizontal, ToggleRight } from 'lucide-react'
import { api, type SignalFunctionDef } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { SIGNAL_COLORS, cnSignal } from '@/lib/signals'

type Direction = 'entry' | 'exit' | 'both'

/** 方向 → 展示元数据。配色与 K 线信号标记（lib/signals 的 SIGNAL_COLORS）保持一致。 */
const DIRECTION_META: Record<Direction, { label: string; color: string }> = {
  entry: { label: '入场', color: SIGNAL_COLORS.entry },
  exit: { label: '离场', color: SIGNAL_COLORS.exit },
  both: { label: '双向', color: SIGNAL_COLORS.both },
}

const DIRECTION_FILTERS: (Direction | 'all')[] = ['all', 'entry', 'exit', 'both']

/** 方向徽标：小圆点 + 文字，颜色取自 SIGNAL_COLORS（与 K 线标记同源）。 */
function DirectionBadge({ direction }: { direction: Direction }) {
  const meta = DIRECTION_META[direction] ?? DIRECTION_META.both
  return (
    <span
      className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium leading-none"
      style={{ color: meta.color, background: `${meta.color}1a`, border: `1px solid ${meta.color}40` }}
    >
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: meta.color }} />
      {meta.label}
    </span>
  )
}

function fieldText(fields: string[]): string {
  if (!fields.length) return '无'
  return fields.map((f) => cnSignal(f)).join(' / ')
}

function paramText(params: SignalFunctionDef['params']): string {
  if (!params.length) return ''
  return params
    .map((p) => {
      const label = p.label ? `${p.label}` : p.id
      const val = p.default != null ? `=${String(p.default)}` : ''
      return `${label}${val}`
    })
    .join('，')
}

export function SignalCatalog() {
  const [query, setQuery] = useState('')
  const [direction, setDirection] = useState<'all' | Direction>('all')

  const { data, isFetching, refetch } = useQuery({
    queryKey: QK.signalCatalog,
    queryFn: api.strategySignals,
    staleTime: 5 * 60_000,
  })

  const signals = data?.signals ?? []

  const { grouped, categories, counts } = useMemo(() => {
    const q = query.trim().toLowerCase()
    const filtered = signals.filter((s) => {
      if (direction !== 'all' && s.direction !== direction) return false
      if (!q) return true
      return (
        s.name.toLowerCase().includes(q) ||
        s.label.toLowerCase().includes(q) ||
        s.description.toLowerCase().includes(q)
      )
    })

    // 保持后端返回顺序（已按 category 排序），分组用 Map 保序。
    const byCategory = new Map<string, SignalFunctionDef[]>()
    for (const s of filtered) {
      const list = byCategory.get(s.category) ?? []
      list.push(s)
      byCategory.set(s.category, list)
    }

    const cnt = { entry: 0, exit: 0, both: 0 }
    for (const s of signals) {
      if (s.direction === 'entry') cnt.entry += 1
      else if (s.direction === 'exit') cnt.exit += 1
      else cnt.both += 1
    }

    return { grouped: byCategory, categories: [...byCategory.keys()], counts: cnt }
  }, [signals, query, direction])

  const totalShown = useMemo(
    () => [...grouped.values()].reduce((acc, list) => acc + list.length, 0),
    [grouped],
  )

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <PageHeader
        title="信号函数库"
        subtitle="借鉴 czsc 信号-事件-交易体系的可复用原子信号 · 组合表达式用 all_of / any_of / not_of 引用"
        right={
          <button
            onClick={() => void refetch()}
            className="flex items-center gap-1 px-2 py-1 text-xs rounded-btn border border-border text-secondary hover:text-foreground hover:bg-elevated cursor-pointer"
          >
            <RefreshCw className={`h-3 w-3 ${isFetching ? 'animate-spin' : ''}`} /> 刷新
          </button>
        }
      />

      {/* 统计条 */}
      <div className="flex flex-wrap items-center gap-4 px-5 py-3 border-b border-border bg-elevated/20">
        <div className="flex items-center gap-1.5 text-xs">
          <Library className="h-3.5 w-3.5 text-muted" />
          <span className="text-secondary">共</span>
          <span className="font-semibold text-foreground num tabular-nums">{signals.length}</span>
          <span className="text-secondary">个信号 · {new Set(signals.map((s) => s.category)).size} 个分类</span>
        </div>
        <div className="flex items-center gap-2 text-[11px]">
          {(Object.keys(DIRECTION_META) as Direction[]).map((d) => (
            <span key={d} className="inline-flex items-center gap-1 text-secondary">
              <span className="h-1.5 w-1.5 rounded-full" style={{ background: DIRECTION_META[d].color }} />
              {DIRECTION_META[d].label}
              <span className="num tabular-nums text-foreground">{counts[d]}</span>
            </span>
          ))}
        </div>
      </div>

      {/* 控制条 */}
      <div className="flex flex-wrap items-center gap-3 px-5 py-2.5 border-b border-border">
        <div className="relative">
          <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索信号名 / 中文名 / 说明"
            className="h-7 w-[240px] pl-7 pr-2 text-xs rounded-btn border border-border bg-surface text-foreground placeholder:text-muted"
          />
        </div>
        <div className="flex items-center gap-1">
          {DIRECTION_FILTERS.map((d) => (
            <button
              key={d}
              type="button"
              onClick={() => setDirection(d)}
              className={`px-2 py-1 text-[11px] rounded-btn border transition-colors cursor-pointer ${
                direction === d
                  ? 'border-accent/50 bg-accent/10 text-accent'
                  : 'border-border text-muted hover:text-foreground hover:bg-elevated'
              }`}
            >
              {d === 'all' ? '全部' : DIRECTION_META[d].label}
            </button>
          ))}
        </div>
        {query || direction !== 'all' ? (
          <span className="text-[11px] text-muted">命中 {totalShown} 个</span>
        ) : null}
      </div>

      {/* 内容 */}
      <div className="flex-1 overflow-y-auto px-5 py-4">
        {!signals.length ? (
          <EmptyState
            icon={Library}
            title="暂无信号函数"
            hint="后端信号函数统一层尚未注册任何信号，或 /api/strategies/signals 未返回数据。"
          />
        ) : categories.length === 0 ? (
          <EmptyState
            icon={Search}
            title="没有匹配的信号"
            hint="换个关键词，或清除方向过滤再试。"
          />
        ) : (
          <div className="space-y-6">
            {categories.map((category) => (
              <section key={category}>
                <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold">
                  {category}
                  <span className="text-[11px] font-normal text-muted">
                    {grouped.get(category)?.length ?? 0} 个
                  </span>
                </h2>
                <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-2.5">
                  {(grouped.get(category) ?? []).map((s) => (
                    <SignalCard key={s.name} signal={s} />
                  ))}
                </div>
              </section>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function SignalCard({ signal: s }: { signal: SignalFunctionDef }) {
  return (
    <div className="flex flex-col gap-1.5 rounded-btn border border-border bg-surface p-3 transition-colors hover:border-accent/30">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <span className="truncate text-sm font-medium text-foreground">{s.label}</span>
            <DirectionBadge direction={s.direction} />
          </div>
          <div className="mt-0.5 truncate font-mono text-[10px] text-muted" title={s.name}>
            {s.name}
          </div>
        </div>
        {s.warmup > 0 && (
          <span
            className="shrink-0 rounded px-1.5 py-0.5 text-[10px] text-muted"
            title={`预热根数：组合时按最大预热根数对齐`}
          >
            预热 {s.warmup}
          </span>
        )}
      </div>

      {s.description && (
        <p className="text-xs text-secondary leading-relaxed">{s.description}</p>
      )}

      <div className="mt-auto space-y-1 text-[11px] text-muted">
        <div className="flex items-center gap-1.5">
          <span className="shrink-0 text-[10px] text-muted/70">依赖</span>
          <span className="text-secondary">{fieldText(s.required_fields)}</span>
        </div>
        {s.params.length > 0 && (
          <div className="flex items-start gap-1.5">
            <SlidersHorizontal className="mt-0.5 h-3 w-3 shrink-0 text-muted/70" />
            <span className="text-secondary">{paramText(s.params)}</span>
          </div>
        )}
        {s.enable_param && (
          <div className="flex items-start gap-1.5">
            <ToggleRight className="mt-0.5 h-3 w-3 shrink-0 text-muted/70" />
            <span className="text-secondary">
              开关参数 <code className="font-mono text-[10px]">{s.enable_param}</code>
              （False 时在组合里中性化）
            </span>
          </div>
        )}
      </div>
    </div>
  )
}
