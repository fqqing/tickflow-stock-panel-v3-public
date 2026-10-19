/**
 * 每日精选(纸面跟踪)页 — 每日 Top N 选股 + 命中率统计。
 *
 * 数据来源: 后端 daily_picks 服务(盘后定时 + 手动触发选股, 纸面跟踪命中率)。
 * 视图: 统计指标卡 + 今日精选卡 + 每日收益柱状图 + 历史命中明细表。
 * A 股语义色: 红涨绿跌(正收益红, 负收益绿)。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import * as echarts from 'echarts'
import { Target, Loader2, TrendingUp, Crosshair, CalendarDays, Sparkles } from 'lucide-react'
import { api, type DailyPickRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useChartTheme } from '@/lib/theme'
import { fmtPct, fmtPrice, priceColorClass } from '@/lib/format'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'

const BULL = '#F04438'  // 红涨
const BEAR = '#12B76A'  // 绿跌

const cardCls = 'rounded-card border border-border bg-surface/80 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm'

function SectionTitle({ icon: Icon, title, hint }: { icon: typeof Target; title: string; hint?: string }) {
  return (
    <div className="flex items-center gap-2">
      <span className="h-3 w-0.5 rounded-full bg-gradient-to-b from-accent to-accent/30" />
      <Icon className="h-3.5 w-3.5 text-accent" />
      <h2 className="text-xs font-semibold text-foreground">{title}</h2>
      {hint != null && <span className="ml-auto text-[10px] text-muted font-mono">{hint}</span>}
    </div>
  )
}

function StatCard({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: 'bull' | 'bear' | 'muted' }) {
  const valueCls = tone === 'bull' ? 'text-bull' : tone === 'bear' ? 'text-bear' : 'text-foreground'
  return (
    <div className={cn(cardCls, 'px-4 py-3')}>
      <div className="text-[11px] text-muted">{label}</div>
      <div className={cn('mt-1 text-2xl font-semibold tabular-nums', valueCls)}>{value}</div>
      {sub != null && <div className="mt-0.5 text-[10px] text-muted">{sub}</div>}
    </div>
  )
}

function useEChart(option: echarts.EChartsOption | null, deps: unknown[]) {
  const ref = useRef<HTMLDivElement>(null)
  const instRef = useRef<echarts.ECharts | null>(null)
  useEffect(() => {
    const onResize = () => instRef.current?.resize()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      instRef.current?.dispose()
      instRef.current = null
    }
  }, [])
  useEffect(() => {
    if (!ref.current) return
    if (!instRef.current) instRef.current = echarts.init(ref.current, undefined, { renderer: 'canvas' })
    if (option) {
      instRef.current.setOption(option, { notMerge: true })
      instRef.current.resize()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [option, ...deps])
  return ref
}

export function DailyPicks() {
  const qc = useQueryClient()
  const ct = useChartTheme()
  const [running, setRunning] = useState(false)

  const today = useQuery({
    queryKey: QK.dailyPicksToday as unknown as string[],
    queryFn: api.dailyPicksToday,
    staleTime: 60 * 1000,
  })
  const history = useQuery({
    queryKey: QK.dailyPicksHistory(200) as unknown as string[],
    queryFn: () => api.dailyPicksHistory(200),
    staleTime: 60 * 1000,
  })
  const stats = useQuery({
    queryKey: QK.dailyPicksStats as unknown as string[],
    queryFn: api.dailyPicksStats,
    staleTime: 60 * 1000,
  })

  const histRows = history.data?.picks ?? []
  const todayPicks = today.data?.picks ?? []

  const trend = useMemo(() => {
    const byDate = new Map<string, number[]>()
    for (const r of histRows) {
      if (r.pnl_pct == null) continue
      const arr = byDate.get(r.pick_date) ?? []
      arr.push(r.pnl_pct)
      byDate.set(r.pick_date, arr)
    }
    return [...byDate.entries()]
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([date, arr]) => ({ date, avg: arr.reduce((a, b) => a + b, 0) / arr.length }))
  }, [histRows])

  const trendOption = useMemo<echarts.EChartsOption | null>(() => {
    if (trend.length === 0) return null
    return {
      grid: { left: 44, right: 16, top: 20, bottom: 28 },
      xAxis: {
        type: 'category',
        data: trend.map(t => t.date.slice(5)),
        axisLine: { lineStyle: { color: ct.border } },
        axisLabel: { color: ct.text, fontSize: 10 },
      },
      yAxis: {
        type: 'value',
        axisLabel: { color: ct.text, fontSize: 10, formatter: (v: number) => `${(v * 100).toFixed(0)}%` },
        splitLine: { lineStyle: { color: ct.grid } },
      },
      tooltip: {
        trigger: 'axis',
        backgroundColor: ct.tooltipBg,
        borderColor: ct.tooltipBorder,
        textStyle: { color: ct.tooltipText, fontSize: 12 },
        valueFormatter: (v: unknown) => `${((v as number) * 100).toFixed(2)}%`,
      },
      series: [{
        type: 'bar',
        barMaxWidth: 18,
        data: trend.map(t => ({ value: t.avg, itemStyle: { color: t.avg >= 0 ? BULL : BEAR } })),
      }],
    }
  }, [trend, ct])

  const trendRef = useEChart(trendOption, [])

  const handleRun = async () => {
    setRunning(true)
    try {
      const res = await api.dailyPicksRun({})
      toast(res.total > 0 ? `已选出 ${res.total} 只` : '本次无策略命中')
      qc.invalidateQueries({ queryKey: ['daily-picks-today'] })
      qc.invalidateQueries({ queryKey: ['daily-picks-history'] })
      qc.invalidateQueries({ queryKey: ['daily-picks-stats'] })
    } catch { /* request 已弹 toast */ } finally {
      setRunning(false)
    }
  }

  const s = stats.data
  const pending = s?.pending ?? 0

  return (
    <div className="mx-auto max-w-6xl space-y-4 p-4">
      {/* 标题 + 操作 */}
      <div className="flex items-center gap-3">
        <span className="h-6 w-1 rounded-full bg-gradient-to-b from-accent to-accent/30" />
        <div className="flex-1">
          <h1 className="text-base font-semibold text-foreground">每日精选</h1>
          <p className="text-[11px] text-muted">
            盘后跑 27 个策略 → 多策略共振选 Top N → 纸面跟踪命中率（持有 {todayPicks[0]?.hold_days ?? 1} 个交易日结算）
          </p>
        </div>
        <button
          onClick={handleRun}
          disabled={running}
          className="flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white hover:bg-accent/90 disabled:opacity-60"
        >
          {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
          {running ? '选股中…' : '立即选股'}
        </button>
      </div>

      {/* 命中率统计 */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard
          label="累计命中率"
          value={s?.hit_rate != null ? `${s.hit_rate.toFixed(1)}%` : '—'}
          sub={`${s?.wins ?? 0} 中 / ${s?.total ?? 0} 已结算`}
          tone={s?.hit_rate != null ? (s.hit_rate >= 50 ? 'bull' : 'bear') : 'muted'}
        />
        <StatCard
          label="平均收益"
          value={s?.avg_pnl_pct != null ? fmtPct(s.avg_pnl_pct) : '—'}
          sub={`中位数 ${s?.median_pnl_pct != null ? fmtPct(s.median_pnl_pct) : '—'}`}
          tone={s?.avg_pnl_pct != null ? (s.avg_pnl_pct >= 0 ? 'bull' : 'bear') : 'muted'}
        />
        <StatCard
          label="强命中（≥2%）"
          value={s?.strong_hits != null ? `${s.strong_hits}` : '—'}
          sub={`止损（≤-3%）${s?.stop_losses ?? 0} 只`}
        />
        <StatCard
          label="待结算"
          value={`${pending}`}
          sub={pending > 0 ? '到期自动结算' : '无未结算条目'}
          tone="muted"
        />
      </div>

      {/* 今日精选 */}
      <div className={cn(cardCls, 'p-4')}>
        <SectionTitle
          icon={Crosshair}
          title="今日精选"
          hint={today.data?.pick_date ? `选股日 ${today.data.pick_date}` : undefined}
        />
        {todayPicks.length === 0 ? (
          <div className="mt-4 rounded-md border border-dashed border-border py-8 text-center text-xs text-muted">
            {today.data?.pick_date ? '今日暂无精选结果' : '尚未选股 — 点击右上角「立即选股」或等盘后自动运行'}
          </div>
        ) : (
          <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {todayPicks.map(p => <PickCard key={p.symbol} p={p} />)}
          </div>
        )}
      </div>

      {/* 每日收益趋势 */}
      <div className={cn(cardCls, 'p-4')}>
        <SectionTitle icon={TrendingUp} title="每日平均收益" hint="按选股日聚合, 已结算" />
        {trend.length === 0 ? (
          <div className="mt-4 rounded-md border border-dashed border-border py-8 text-center text-xs text-muted">
            暂无已结算数据 — 选股持有到期后自动生成
          </div>
        ) : (
          <div ref={trendRef} className="mt-3 h-48" />
        )}
      </div>

      {/* 历史明细 */}
      <div className={cn(cardCls, 'p-4')}>
        <SectionTitle icon={CalendarDays} title="历史明细" hint={`${histRows.length} 条`} />
        {histRows.length === 0 ? (
          <div className="mt-4 rounded-md border border-dashed border-border py-8 text-center text-xs text-muted">
            暂无历史记录
          </div>
        ) : (
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-muted">
                  <th className="pb-2 pr-3 font-normal">选股日</th>
                  <th className="pb-2 pr-3 font-normal">代码</th>
                  <th className="pb-2 pr-3 font-normal">名称</th>
                  <th className="pb-2 pr-3 font-normal">共振</th>
                  <th className="pb-2 pr-3 font-normal text-right">买入参考</th>
                  <th className="pb-2 pr-3 font-normal text-right">结算价</th>
                  <th className="pb-2 pr-3 font-normal text-right">收益</th>
                  <th className="pb-2 font-normal">命中</th>
                </tr>
              </thead>
              <tbody>
                {histRows.map(r => (
                  <tr key={`${r.pick_date}-${r.symbol}`} className="border-t border-border/60">
                    <td className="py-2 pr-3 font-mono text-muted">{r.pick_date}</td>
                    <td className="py-2 pr-3 font-mono text-foreground">
                      <Link to={`/stock/${encodeURIComponent(r.symbol)}`} className="hover:text-accent">{r.symbol}</Link>
                    </td>
                    <td className="py-2 pr-3 text-foreground">
                      <Link to={`/stock/${encodeURIComponent(r.symbol)}`} className="hover:text-accent">{r.name ?? '—'}</Link>
                    </td>
                    <td className="py-2 pr-3 tabular-nums text-foreground">{r.signal_count}</td>
                    <td className="py-2 pr-3 text-right tabular-nums text-foreground">{fmtPrice(r.entry_close)}</td>
                    <td className="py-2 pr-3 text-right tabular-nums text-foreground">{fmtPrice(r.exit_close)}</td>
                    <td className={cn('py-2 pr-3 text-right font-medium tabular-nums', priceColorClass(r.pnl_pct))}>
                      {fmtPct(r.pnl_pct)}
                    </td>
                    <td className="py-2">{hitBadge(r)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

function PickCard({ p }: { p: DailyPickRow }) {
  const hits = useMemo(() => {
    try { return JSON.parse(p.strategy_hits ?? '[]') as string[] } catch { return [] }
  }, [p.strategy_hits])
  return (
    <Link
      to={`/stock/${encodeURIComponent(p.symbol)}`}
      className="group rounded-md border border-border bg-base/60 p-3 transition-colors hover:border-accent/50 hover:bg-elevated/40"
      title={`查看 ${p.symbol} ${p.name ?? ''} K 线`}
    >
      <div className="flex items-center gap-2">
        <span className="flex h-5 w-5 items-center justify-center rounded bg-accent/15 text-[11px] font-semibold text-accent">
          {p.rank}
        </span>
        <span className="font-mono text-sm font-medium text-foreground group-hover:text-accent">{p.symbol}</span>
        <span className="text-xs text-foreground/80">{p.name}</span>
        <span className="ml-auto text-[10px] text-muted">共振 {p.signal_count}</span>
      </div>
      <div className="mt-2 flex items-baseline justify-between">
        <span className="text-[11px] text-muted">买入参考（收盘）</span>
        <span className="text-sm font-medium tabular-nums text-foreground">{fmtPrice(p.entry_close)}</span>
      </div>
      {p.score != null && (
        <div className="mt-1 flex items-baseline justify-between">
          <span className="text-[11px] text-muted">综合评分</span>
          <span className="text-sm font-medium tabular-nums text-accent">{p.score.toFixed(1)}</span>
        </div>
      )}
      {hits.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {hits.slice(0, 4).map(h => (
            <span key={h} className="rounded bg-accent/10 px-1.5 py-0.5 font-mono text-[10px] text-accent">{h}</span>
          ))}
          {hits.length > 4 && <span className="text-[10px] text-muted">+{hits.length - 4}</span>}
        </div>
      )}
    </Link>
  )
}

function hitBadge(r: DailyPickRow) {
  if (r.pnl_pct == null) {
    return <span className="rounded bg-muted/10 px-1.5 py-0.5 text-[10px] text-muted">待结算</span>
  }
  return r.hit ? (
    <span className="rounded bg-bull/10 px-1.5 py-0.5 text-[10px] text-bull">命中</span>
  ) : (
    <span className="rounded bg-bear/10 px-1.5 py-0.5 text-[10px] text-bear">未中</span>
  )
}
