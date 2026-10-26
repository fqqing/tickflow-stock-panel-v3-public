/**
 * 主线页 — 主线梯队全景 / 主线转换嗅探 / 快报今日热点。
 *
 * 三个板块都由后端 /api/hotspot/* 纯增量缝合而来, 复用已落盘资产:
 *   - mainline:     主线分排行 → 龙头/前排(2板+)/中排(首板)/后排(跟风)
 *   - rotation:     涨幅排名矩阵 rising/fading 确定性信号 + 主线分轨迹交叉印证
 *   - flash-summary:东财 7x24 快讯按题材聚类 + 关联个股
 */
import { useQuery } from '@tanstack/react-query'
import { Loader2, Route as RouteIcon, TrendingUp, TrendingDown, Crown, Flame, Newspaper, Repeat } from 'lucide-react'
import {
  api,
  type RotationSniff,
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

export function Mainline() {
  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="主线"
        subtitle="主线梯队全景 · 转换嗅探 · 今日热点（纯增量缝合，复用已有数据资产）"
        titleExtra={<RouteIcon className="h-4 w-4 text-muted" />}
      />
      <div className="grid flex-1 gap-4 overflow-auto p-4 md:grid-cols-2 xl:grid-cols-3">
        <Card title="主线梯队全景" icon={<Crown className="h-4 w-4" />}><MainlinePanoramaView /></Card>
        <Card title="主线转换嗅探" icon={<Repeat className="h-4 w-4" />}><RotationSniffView /></Card>
        <Card title="快报 · 今日热点" icon={<Newspaper className="h-4 w-4" />}><FlashHotspotView /></Card>
      </div>
    </div>
  )
}
