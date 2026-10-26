/**
 * 主线页 — 主线梯队全景 / 主线转换嗅探 / 快报今日热点。
 *
 * 三个板块都由后端 /api/hotspot/* 纯增量缝合而来, 复用已落盘资产:
 *   - mainline:     主线分排行 → 龙头/前排(2板+)/中排(首板)/后排(跟风)
 *   - rotation:     涨幅排名矩阵 rising/fading 确定性信号 + 主线分轨迹交叉印证
 *   - flash-summary:东财 7x24 快讯按题材聚类 + 关联个股
 */
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Loader2, Route as RouteIcon, TrendingUp, TrendingDown, Crown, Flame, Newspaper, Repeat, CalendarDays, Lightbulb, ArrowLeftRight } from 'lucide-react'
import {
  api,
  type RotationSniff,
  type MainlineCalendarCell,
  type MainlineCompareItem,
} from '@/lib/api'
import { PageHeader } from '@/components/PageHeader'
import { priceColorClass } from '@/lib/format'
import { cn } from '@/lib/cn'

function pctText(v: number | undefined | null): string {
  if (v == null || Number.isNaN(v)) return '—'
  const sign = v > 0 ? '+' : ''
  return `${sign}${v.toFixed(1)}%`
}

function Card({ title, icon, children }: { title: string; icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-border bg-card/60">
      <div className="flex items-center gap-2 border-b border-border px-4 py-2.5">
        <span className="text-muted">{icon}</span>
        <h2 className="text-sm font-semibold">{title}</h2>
      </div>
      <div className="p-4">{children}</div>
    </section>
  )
}

function StockChip({ symbol, name, extra, extraClass }: {
  symbol: string; name?: string; extra?: string; extraClass?: string
}) {
  return (
    <span className="inline-flex items-center gap-1 rounded border border-border bg-card px-2 py-0.5 text-xs">
      <span className="font-medium">{name || symbol.split('.')[0]}</span>
      {extra && <span className={cn('text-xs', extraClass ?? 'text-muted')}>{extra}</span>}
    </span>
  )
}

function MainlinePanoramaView() {
  const q = useQuery({
    queryKey: ['hotspot-mainline'],
    queryFn: () => api.mainlinePanorama(8, 'concept'),
  })
  if (q.isLoading) return <div className="flex items-center gap-2 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" />主线计算中…</div>
  if (q.isError) return <div className="text-sm text-muted">主线数据加载失败</div>
  const data = q.data
  if (!data || !data.mainlines.length) {
    return <div className="text-sm text-muted">暂无主线数据 — 请先在「市场环境」页重算主线, 或等盘后管道落盘。</div>
  }
  return (
    <div className="space-y-3">
      <div className="text-xs text-muted">{data.date ? `截至 ${data.date} 收盘` : ''}{data.note ? ` · ${data.note}` : ''}</div>
      {data.mainlines.map(m => (
        <div key={m.member} className="rounded-md border border-border bg-background/40 p-3">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-xs font-semibold text-amber-400">#{m.rank} 主线</span>
            <span className="text-sm font-semibold">{m.member}</span>
            <span className="text-xs text-muted">主线分 {m.score} · 涨停 {m.limit_up_count} 家 · 最高 {m.max_boards} 板</span>
          </div>
          <div className="mb-1 flex flex-wrap items-center gap-1.5">
            <span className="flex items-center gap-1 text-xs text-muted"><Crown className="h-3.5 w-3.5 text-red-400" />龙头</span>
            {m.leader ? <StockChip symbol={m.leader.symbol} name={m.leader.name} extra={`${m.leader.boards}板`} extraClass="text-red-400" /> : <span className="text-xs text-muted">—</span>}
          </div>
          {m.front.length > 0 && (
            <div className="mb-1 flex flex-wrap items-center gap-1.5">
              <span className="flex items-center gap-1 text-xs text-muted"><Flame className="h-3.5 w-3.5 text-amber-400" />前排(2板+)</span>
              {m.front.map(s => <StockChip key={s.symbol} symbol={s.symbol} name={s.name} extra={`${s.boards}板`} extraClass="text-amber-400" />)}
            </div>
          )}
          {m.mid.length > 0 && (
            <div className="mb-1 flex flex-wrap items-center gap-1.5">
              <span className="text-xs text-muted">中排(首板)</span>
              {m.mid.map(s => <StockChip key={s.symbol} symbol={s.symbol} name={s.name} />)}
            </div>
          )}
          {m.rear.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-xs text-muted">后排(跟风)</span>
              {m.rear.map(s => (
                <StockChip key={s.symbol} symbol={s.symbol} name={s.name}
                  extra={pctText(s.change_pct)} extraClass={priceColorClass(s.change_pct)} />
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

function RotationSniffView() {
  const q = useQuery({
    queryKey: ['hotspot-rotation'],
    queryFn: () => api.rotationSniff(12, 'concept'),
  })
  if (q.isLoading) return <div className="flex items-center gap-2 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" />轮动信号计算中…</div>
  if (q.isError) return <div className="text-sm text-muted">轮动数据加载失败</div>
  const d = q.data
  if (!d || (!d.ascending.length && !d.fading.length && !d.persistent.length)) {
    return <div className="text-sm text-muted">暂无轮动信号 — 请先在「概念分析」页获取概念数据源。</div>
  }
  const signalRow = (label: string, icon: React.ReactNode, items: RotationSniff['ascending'], cls: string) => (
    <div className="mb-2">
      <div className={cn('mb-1 flex items-center gap-1 text-xs font-semibold', cls)}>{icon}{label}</div>
      {items.length === 0
        ? <div className="text-xs text-muted">(本类无明显信号)</div>
        : items.map(it => (
            <div key={it.member} className="flex flex-wrap items-center gap-2 text-xs">
              <span className="font-medium">{it.member}</span>
              <span className="text-muted">排名 {it.ranks.filter(r => r < 999).join('→')}</span>
              <span className="text-muted">均排名 {it.avg_rank} · 波动σ {it.rank_std}</span>
              {it.mainline_delta != null && (
                <span className={priceColorClass(it.mainline_delta)}>主线分 {it.mainline_delta > 0 ? '+' : ''}{it.mainline_delta}</span>
              )}
            </div>
          ))}
    </div>
  )
  return (
    <div>
      {signalRow('▲ 可能转主线', <TrendingUp className="h-3.5 w-3.5" />, d.ascending, 'text-red-400')}
      {signalRow('▼ 可能退潮', <TrendingDown className="h-3.5 w-3.5" />, d.fading, 'text-emerald-400')}
      {signalRow('★ 连续霸榜(主线确认)', <Crown className="h-3.5 w-3.5" />, d.persistent, 'text-amber-400')}
    </div>
  )
}

function FlashHotspotView() {
  const q = useQuery({
    queryKey: ['hotspot-flash'],
    queryFn: () => api.flashHotspot(60, 12),
    refetchInterval: 60_000,
  })
  if (q.isLoading) return <div className="flex items-center gap-2 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" />快报聚合中…</div>
  if (q.isError) return <div className="text-sm text-muted">快报加载失败</div>
  const d = q.data
  if (!d || !d.hotspots.length) return <div className="text-sm text-muted">暂无快报数据。</div>
  return (
    <div>
      <div className="mb-2 text-xs text-muted">
        {d.date} · 东财 7×24 快讯按题材聚类 · 自动关联个股
        {d.summary_source === 'ai' && <span className="ml-1 text-amber-400"> · AI 归纳</span>}
      </div>
      {d.summary && (
        <pre className="mb-3 whitespace-pre-wrap rounded-md border border-border bg-background/40 p-3 font-sans text-sm leading-relaxed">{d.summary}</pre>
      )}
      <ol className="space-y-2">
        {d.hotspots.map((h, i) => (
          <li key={i} className="rounded-md border border-border bg-background/40 p-2.5">
            <div className="text-sm">
              {h.important && <span className="mr-1 rounded bg-red-500/15 px-1 text-xs text-red-400">重</span>}
              <span className="font-medium">{i + 1}. {h.title}</span>
              {h.topic !== '要闻' && <span className="ml-2 rounded bg-amber-500/10 px-1 text-xs text-amber-400">[{h.topic}]</span>}
            </div>
            {h.stocks.length > 0 && (
              <div className="mt-1.5 flex flex-wrap gap-1">
                {h.stocks.map(s => <StockChip key={s.symbol} symbol={s.symbol} name={s.name} />)}
              </div>
            )}
          </li>
        ))}
      </ol>
    </div>
  )
}

function MainlineReasonView() {
  const q = useQuery({
    queryKey: ['hotspot-mainline-reason'],
    queryFn: () => api.mainlineReason(8, 'concept'),
  })
  if (q.isLoading) return <div className="flex items-center gap-2 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" />主线原因分析中…</div>
  if (q.isError) return <div className="text-sm text-muted">主线原因加载失败</div>
  const d = q.data
  if (!d || !d.reasons.length) return <div className="text-sm text-muted">暂无主线原因数据。</div>
  return (
    <div className="space-y-3">
      <div className="text-xs text-muted">{d.date ? `截至 ${d.date} 收盘` : ''}{d.note ? ` · ${d.note}` : ''}</div>
      {d.reasons.map(r => (
        <div key={r.member} className="rounded-md border border-border bg-background/40 p-3">
          <div className="mb-1.5 flex flex-wrap items-center gap-2">
            <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-xs font-semibold text-amber-400">#{r.rank}</span>
            <span className="text-sm font-semibold">{r.member}</span>
            <span className="text-xs text-muted">主线分 {r.score} · 涨停 {r.limit_up_count} 家</span>
            {r.reason_source === 'ai' && <span className="rounded bg-amber-500/10 px-1 text-xs text-amber-400">AI</span>}
          </div>
          <div className="text-sm leading-relaxed">{r.reason}</div>
          {r.ths_count > 0 && (
            <div className="mt-2 space-y-1">
              <div className="flex items-center gap-1 text-xs text-muted">
                <Flame className="h-3.5 w-3.5 text-amber-400" />
                同花顺涨停原因命中 {r.ths_count} 家
              </div>
              <div className="flex flex-wrap gap-1.5">
                {r.ths_hits.map((h, i) => (
                  <span key={i} title={h.reason}
                    className="inline-flex items-center gap-1.5 rounded border border-border bg-card px-2 py-0.5 text-xs">
                    <span className="font-medium">{h.name}</span>
                    <span className="text-muted">{h.reason}</span>
                  </span>
                ))}
              </div>
            </div>
          )}
          {r.catalysts.length > 0 && (
            <div className="mt-2 space-y-1">
              {r.catalysts.map((c, i) => (
                <div key={i} className="flex items-start gap-1.5 text-xs text-muted">
                  {c.important && <span className="shrink-0 rounded bg-red-500/15 px-1 text-xs leading-4 text-red-400">重</span>}
                  <span className="line-clamp-2">{c.title}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

function consensusBadge(c: MainlineCompareItem['consensus']) {
  if (c === 'both') return <span className="rounded bg-red-500/15 px-1.5 py-0.5 text-xs font-semibold text-red-400">双确认</span>
  if (c === 'ours_only') return <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-xs font-semibold text-amber-400">仅自算</span>
  return <span className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted">未匹配</span>
}

function MainlineCompareView() {
  const q = useQuery({
    queryKey: ['hotspot-mainline-compare'],
    queryFn: () => api.mainlineCompare(8, 'concept', 30),
  })
  if (q.isLoading) return <div className="flex items-center gap-2 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" />板块对比中…</div>
  if (q.isError) return <div className="text-sm text-muted">板块榜加载失败</div>
  const d = q.data
  if (!d || (!d.mainlines.length && !d.plate_top.length)) return <div className="text-sm text-muted">暂无对比数据。</div>
  return (
    <div className="space-y-3">
      <div className="text-xs text-muted">自算主线 vs 腾讯板块涨幅榜（共 {d.plate_total} 板块）· 双确认 = 两边都强{d.note ? ` · ${d.note}` : ''}</div>
      <div className="space-y-1.5">
        {d.mainlines.map(m => (
          <div key={m.member} className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-background/40 p-2">
            <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-xs font-semibold text-amber-400">#{m.rank}</span>
            <span className="text-sm font-medium">{m.member}</span>
            {consensusBadge(m.consensus)}
            <span className="ml-auto text-xs text-muted">
              {m.plate_name ? <span className="mr-1">{m.plate_name}</span> : null}
              {m.plate_change_pct != null ? <span className={priceColorClass(m.plate_change_pct)}>{pctText(m.plate_change_pct)}</span> : <span>腾讯榜未匹配</span>}
              {m.plate_rank != null ? <span> · 腾讯第{m.plate_rank}</span> : null}
            </span>
          </div>
        ))}
      </div>
      {d.plate_top.length > 0 && (
        <div>
          <div className="mb-1 text-xs font-semibold text-muted">腾讯涨幅榜前 {d.plate_top.length}（未覆盖 = 潜在新主线）</div>
          <div className="space-y-1">
            {d.plate_top.map((e, i) => (
              <div key={`${e.name}-${i}`} className="flex flex-wrap items-center gap-2 text-xs">
                <span className="w-5 text-muted">{i + 1}</span>
                <span className="font-medium">{e.name}</span>
                <span className={priceColorClass(e.change_pct)}>{pctText(e.change_pct)}</span>
                {e.leader && <span className="text-muted">领涨 {e.leader}</span>}
                {e.in_ours
                  ? <span className="text-muted">→ 已在主线（{e.ours_member}）</span>
                  : <span className="rounded bg-emerald-500/15 px-1 text-emerald-400">未覆盖</span>}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function cellTitle(c: MainlineCalendarCell): string {
  return [
    c.member,
    `主线分 ${c.score}`,
    `涨停 ${c.limit_up_count} 家 · 最高 ${c.max_boards} 板`,
    c.leader_name ? `龙头 ${c.leader_name}` : '',
    c.streak > 1 ? `连续上榜 ${c.streak} 天` : '',
  ].filter(Boolean).join('\n')
}

/** 主线分(0-100) → 红色热力(红涨配色): 分越高底色越浓 */
function cellBg(score: number): string {
  const alpha = (0.05 + Math.min(Math.max(score, 0), 100) / 100 * 0.45).toFixed(3)
  return `rgba(239, 68, 68, ${alpha})`
}

function MainlineCalendarView() {
  const [days, setDays] = useState(30)
  const [kind, setKind] = useState<'concept' | 'industry'>('concept')
  const q = useQuery({
    queryKey: ['hotspot-mainline-calendar', days, kind],
    queryFn: () => api.mainlineCalendar(days, 8, kind),
  })
  if (q.isLoading) return <div className="flex items-center gap-2 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" />周期表加载中…</div>
  if (q.isError) return <div className="text-sm text-muted">周期表加载失败</div>
  const d = q.data
  if (!d || !d.dates.length) return <div className="text-sm text-muted">暂无主线历史 — 请先在「市场环境」页重算主线。</div>
  const dateLabel = (iso: string) => iso.slice(5).replace('-', '/')
  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-muted">
        <span>往日每日主线排行 · 近 {d.dates.length} 个交易日{d.note ? ` · ${d.note}` : ''}</span>
        <span className="flex-1" />
        {([10, 20, 30] as const).map(n => (
          <button key={n} onClick={() => setDays(n)}
            className={cn('rounded px-1.5 py-0.5', days === n ? 'bg-red-500/20 font-semibold text-red-400' : 'text-muted hover:text-foreground')}>
            {n}日
          </button>
        ))}
        <span className="mx-1 h-3 w-px bg-border" />
        {(['concept', 'industry'] as const).map(k => (
          <button key={k} onClick={() => setKind(k)}
            className={cn('rounded px-1.5 py-0.5', kind === k ? 'bg-amber-500/20 font-semibold text-amber-400' : 'text-muted hover:text-foreground')}>
            {k === 'concept' ? '概念' : '行业'}
          </button>
        ))}
      </div>
      <div className="overflow-x-auto rounded-md border border-border">
        <table className="w-full border-collapse text-xs">
          <thead>
            <tr className="bg-card">
              <th className="sticky left-0 z-10 border-b border-r border-border bg-card px-2 py-1.5 text-left font-medium text-muted">名次</th>
              {d.dates.map(dt => (
                <th key={dt} className={cn('border-b border-border px-1.5 py-1.5 font-medium text-muted whitespace-nowrap',
                  dt === d.dates[d.dates.length - 1] && 'text-red-400')}>
                  {dateLabel(dt)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {d.rows.map(row => (
              <tr key={row.rank}>
                <td className="sticky left-0 z-10 border-b border-r border-border bg-card px-2 py-1 whitespace-nowrap">
                  <span className={cn('font-semibold', row.rank <= 3 ? 'text-red-400' : 'text-muted')}>第{row.rank}名</span>
                </td>
                {row.cells.map((c, i) => (
                  <td key={i} className="border-b border-border p-0.5 align-top">
                    {c ? (
                      <div title={cellTitle(c)}
                        style={{ backgroundColor: cellBg(c.score) }}
                        className="relative min-w-[5.5rem] rounded-sm px-1.5 py-1">
                        <div className="flex items-start justify-between gap-1">
                          <span className="line-clamp-2 leading-tight font-medium">{c.member}</span>
                          {c.streak >= 3 && <span className="shrink-0 rounded bg-amber-500/25 px-0.5 text-[10px] leading-4 font-semibold text-amber-500">×{c.streak}</span>}
                          {c.is_new && <span className="shrink-0 rounded bg-red-500/30 px-0.5 text-[10px] leading-4 text-red-400">新</span>}
                        </div>
                        <div className="mt-0.5 flex items-center gap-1 text-[10px] text-muted">
                          <span>{c.score}</span>
                          {c.max_boards > 0 && <span className="text-red-400">{c.max_boards}板</span>}
                        </div>
                      </div>
                    ) : <div className="min-h-[2.1rem]" />}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {d.member_summary.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <span className="text-xs text-muted">近{d.dates.length}日霸榜:</span>
          {d.member_summary.slice(0, 8).map(s => (
            <span key={s.member} className="inline-flex items-center gap-1 rounded border border-border bg-card px-1.5 py-0.5 text-xs">
              <span className="font-medium">{s.member}</span>
              <span className="text-muted">{s.days_on}天</span>
              {s.streak >= 3 && <span className="text-amber-500">连{s.streak}</span>}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

export function Mainline() {
  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="主线"
        subtitle="板块周期表 · 主线梯队全景 · 转换嗅探 · 今日热点（纯增量缝合，复用已有数据资产）"
        titleExtra={<RouteIcon className="h-4 w-4 text-muted" />}
      />
      <div className="flex-1 space-y-4 overflow-auto p-4">
        <Card title="板块周期表（往日主线 30 天）" icon={<CalendarDays className="h-4 w-4" />}>
          <MainlineCalendarView />
        </Card>
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          <Card title="主线梯队全景" icon={<Crown className="h-4 w-4" />}><MainlinePanoramaView /></Card>
          <Card title="主线原因" icon={<Lightbulb className="h-4 w-4" />}><MainlineReasonView /></Card>
          <Card title="主线转换嗅探" icon={<Repeat className="h-4 w-4" />}><RotationSniffView /></Card>
          <Card title="腾讯板块对比" icon={<ArrowLeftRight className="h-4 w-4" />}><MainlineCompareView /></Card>
          <Card title="快报 · 今日热点" icon={<Newspaper className="h-4 w-4" />}><FlashHotspotView /></Card>
        </div>
      </div>
    </div>
  )
}
