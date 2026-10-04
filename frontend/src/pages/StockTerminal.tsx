/**
 * 个股终端(全屏工作区) —— P0 容器 + P2 终端形态。
 *
 * P0: 独立路由 /stock/:symbol, 占满视口, 图表高度自适应, 价格单一源, 承载触发上下文。
 * P2: 三栏可折叠工作区 + 键盘优先 + 响应式三档
 *       >=1440  左栏(股票轨道) + 图表 + 右栏(盘口)
 *       1024-1440  图表 + 右栏(左栏收为抽屉)
 *       <1024   图表全宽(左右都收为抽屉)
 *
 * 图表内核: 默认 ECharts(StockPanel), 灰度开关可切到 KLineChart(KLinePro, P1)。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useParams, useNavigate, useLocation } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import {
  alertPointsToEvents,
  mergeEventPoints,
  tradesToEvents,
} from '@/lib/chart-events'
import { QK } from '@/lib/queryKeys'
import { useQuote, useElementHeight } from '@/lib/useQuote'
import { usePreferences, useQuoteStatus } from '@/lib/useSharedQueries'
import { setFocusSymbol, clearFocusSymbol } from '@/lib/useQuoteStream'
import { useLayoutMode } from '@/lib/useLayoutMode'
import { useRecentStocks } from '@/lib/useRecentStocks'
import { StockPanel, getDefaultRange } from '@/components/StockPanel'
import { fallbackPeriod, RENDERERS, type ChartRendererId } from '@/lib/chartRenderer'
import { applyWorkspace, chartSession, useChartSession, WORKSPACE_PRESETS } from '@/lib/chartSession'
import { chartBars, chartFocus, chartSignals } from '@/lib/chartBridge'
import { eventPointsToTimeline, mergeTimeline, type TimelineEvent } from '@/lib/chart-timeline'
import { EventTimeline } from '@/components/kline/EventTimeline'
import { DepthPanel } from '@/components/DepthPanel'
import { DatePicker } from '@/components/DatePicker'
import { RuleEditor } from '@/components/monitor/RuleEditor'
import { TerminalHeader } from '@/components/stock-terminal/TerminalHeader'
import { ContextRibbon, type TriggerContext } from '@/components/stock-terminal/ContextRibbon'
import { StockRail, type RailItem } from '@/components/stock-terminal/StockRail'
import { CommandPalette } from '@/components/stock-terminal/CommandPalette'
import { ShortcutHelp } from '@/components/stock-terminal/ShortcutHelp'
import { KLinePro } from '@/components/kline/KLinePro'
import { getKLineProFlag, setKLineProFlag } from '@/components/kline/useKLineProFlag'
import type { ChartPriceLine } from '@/components/EChartsCandlestick'
import { cn } from '@/lib/cn'

const PRESETS: { label: string; months: number }[] = [
  { label: '近1月', months: 1 },
  { label: '近3月', months: 3 },
  { label: '近6月', months: 6 },
  { label: '近1年', months: 12 },
  { label: '近3年', months: 36 },
]

function iso(d: Date): string {
  return d.toISOString().slice(0, 10)
}

/** 区间选择条 —— 从旧弹窗顶栏下沉到图表上方 */
function RangeBar({
  value,
  onChange,
}: {
  value: { start: string; end: string }
  onChange: (v: { start: string; end: string }) => void
}) {
  const pick = (months: number) => {
    const end = new Date()
    const s = new Date()
    s.setMonth(s.getMonth() - months)
    onChange({ start: iso(s), end: iso(end) })
  }
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-1.5">
      {PRESETS.map(p => {
        const s = new Date()
        s.setMonth(s.getMonth() - p.months)
        const active = value.start === iso(s)
        return (
          <button
            key={p.label}
            type="button"
            onClick={() => pick(p.months)}
            className={cn(
              'h-6 rounded border px-1.5 text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent',
              active
                ? 'border-accent/30 bg-accent/20 font-medium text-accent'
                : 'border-transparent text-muted hover:bg-elevated hover:text-foreground',
            )}
          >
            {p.label}
          </button>
        )
      })}
      <DatePicker value={value.start} onChange={v => onChange({ ...value, start: v })} max={value.end} />
      <span className="text-[10px] text-muted/40">~</span>
      <DatePicker value={value.end} onChange={v => onChange({ ...value, end: v })} min={value.start} />
    </div>
  )
}

/** 抽屉入口小按钮 */
function DrawerButton({ side, label, onClick }: { side: 'left' | 'right'; label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={`展开${label}`}
      className="h-6 shrink-0 rounded border border-border bg-elevated px-1.5 text-[11px] text-muted transition-colors hover:text-foreground"
    >
      {side === 'left' ? '› ' : ''}{label}{side === 'right' ? ' ‹' : ''}
    </button>
  )
}

/** 叠加层开关 —— 终端层的唯一入口(内核工具条在 hideOverlayToggles 下不再重复渲染) */
function OverlayToggle({
  active,
  label,
  title,
  onClick,
}: {
  active: boolean
  label: string
  title: string
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className={cn(
        'h-6 rounded border px-2 text-[11px] font-mono transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent',
        active ? 'border-accent/30 bg-accent/20 text-accent' : 'border-transparent text-muted hover:bg-elevated',
      )}
    >
      {label}
    </button>
  )
}

export function StockTerminal() {
  const { symbol = '' } = useParams<{ symbol: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const qc = useQueryClient()

  const { data: prefs } = usePreferences()
  const { data: quoteStatus } = useQuoteStatus()
  const realtimeRunning = quoteStatus?.running ?? false
  const intradayRefreshOn = prefs?.minute_intraday_refresh ?? false
  const refetchMs = intradayRefreshOn && realtimeRunning
    ? (prefs?.minute_intraday_refresh_interval ?? 6) * 1000
    : undefined

  // 全屏唯一的价格源(与 DepthPanel 共享缓存)
  const quote = useQuote(symbol, refetchMs)

  const [dateRange, setDateRange] = useState(() => getDefaultRange())
  /**
   * 图表会话: 周期 / 复权 / 叠加层开关住在组件树之外(见 lib/chartSession)。
   * 这里不再用 useState —— 否则切内核时子树卸载重建会把它们一起带走,
   * 那正是「切换割裂」的根因。现在切内核只是换渲染器 + 重放会话。
   */
  const session = useChartSession()
  const { period, adjust, overlays } = session
  const structOn = overlays.structure
  const chanOn = overlays.chan
  const chipsOn = overlays.chips
  const signalsOn = overlays.signals
  const alertsOn = overlays.alerts
  const tradesOn = overlays.trades
  const [renderer, setRenderer] = useState<ChartRendererId>(
    () => (getKLineProFlag() ? 'klinecharts' : 'echarts'),
  )
  const useKLine = renderer === 'klinecharts'
  /** 当前渲染器的能力矩阵: UI 按它显隐, 不按内核名分叉 */
  const caps = RENDERERS[renderer].capabilities
  const [priceLines, setPriceLines] = useState<ChartPriceLine[]>([])
  const [showMonitor, setShowMonitor] = useState(false)
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [helpOpen, setHelpOpen] = useState(false)

  // ── P2 布局 ────────────────────────────────────────────────
  const mode = useLayoutMode()
  const [railOpen, setRailOpen] = useState(() => mode === 'three')
  const [depthOpen, setDepthOpen] = useState(() => mode !== 'one')
  const prevMode = useRef(mode)
  useEffect(() => {
    if (prevMode.current === mode) return
    prevMode.current = mode
    setRailOpen(mode === 'three')
    setDepthOpen(mode !== 'one')
  }, [mode])
  const railDocked = mode === 'three' && railOpen
  const railDrawer = mode !== 'three' && railOpen
  const depthDocked = mode !== 'one' && depthOpen
  const depthDrawer = mode === 'one' && depthOpen

  // 图表高度随窗口自适应
  const boxRef = useRef<HTMLDivElement>(null)
  const chartHeight = useElementHeight(boxRef, 92, 260)

  // 焦点股票注册: SSE 推送时精准刷新当前股票日K
  useEffect(() => {
    if (!symbol) return
    setFocusSymbol(symbol)
    return () => clearFocusSymbol()
  }, [symbol])

  // ── 外部事件标记(监控触发 / 回测买卖点) ──────────────────────
  // 开关关着就不取数(这两路都不是每只票都有数据); 也只在日线档取 —— 周/月线的
  // date 是周末, 日粒度的告警/买卖点对不上, 取回来也画不出来。
  // 归约在终端层做完再下发, 两个内核拿的是同一份, 不会各画一套。
  const alertsQ = useQuery({
    queryKey: QK.chartAlerts(symbol, 7),
    queryFn: () => api.alertsBySymbol(symbol, 7),
    enabled: !!symbol && alertsOn && period === 'day',
    staleTime: 60_000,
  })
  const tradesQ = useQuery({
    queryKey: QK.chartTrades(symbol),
    queryFn: () => api.lastBacktestTrades(symbol),
    enabled: !!symbol && tradesOn && period === 'day',
    staleTime: 300_000,
  })
  // 拆成两步: 归约结果要同时喂给「K 线标记」和「事件时间轴」两个消费者,
  // 各自归约一遍就会长出两套实现(正是之前切内核割裂的老病根)。
  const alertPoints = useMemo(
    () => alertPointsToEvents(alertsQ.data?.points ?? []),
    [alertsQ.data],
  )
  const tradePoints = useMemo(
    () => tradesToEvents(tradesQ.data?.trades ?? []),
    [tradesQ.data],
  )
  const eventMarks = useMemo(
    () => mergeEventPoints(
      alertsOn ? alertPoints : [],
      tradesOn ? tradePoints : [],
    ),
    [alertsOn, alertPoints, tradesOn, tradePoints],
  )

  // ── 事件时间轴(P4-1) ───────────────────────────────────────
  // 日期序列与信号事件由渲染器经窄通道上报(见 lib/chartBridge): 时间轴挂在终端
  // 层, 拿不到渲染器内部的 rows, 而让终端层再拉一次日K是重复请求。
  const [barDates, setBarDates] = useState<string[]>([])
  useEffect(() => chartBars.subscribe(setBarDates), [])
  const [signalEvents, setSignalEvents] = useState<TimelineEvent[]>([])
  useEffect(() => chartSignals.subscribe(setSignalEvents), [])
  const timelineOn = overlays.timeline
  /**
   * ★ 时间轴显示的是**当前已启用的那几类事件**, 与图上标记一一对应 —— 绝不出现
   *   「条上有、图上没有」。所以打开时间轴会连带打开「信号」(见 toggleTimeline):
   *   否则条是空的, 而图上却一个标记也没有, 用户只会觉得这个功能坏了。
   */
  const timelineEvents = useMemo(
    () => mergeTimeline(
      signalsOn ? signalEvents : [],
      alertsOn ? eventPointsToTimeline(alertPoints, 'alert') : [],
      tradesOn ? eventPointsToTimeline(tradePoints, 'trade') : [],
    ),
    [signalsOn, signalEvents, alertsOn, alertPoints, tradesOn, tradePoints],
  )
  const toggleTimeline = useCallback((v: boolean) => {
    const cur = chartSession.getState().overlays
    // 一次 patch 改两个开关: 分两次会通知两遍, 中间那帧状态是自相矛盾的
    chartSession.patch({
      overlays: { ...cur, timeline: v, signals: v ? true : cur.signals },
    })
  }, [])

  // 会话里记一份当前标的(便于排查, 也为将来「按 symbol 隔离视口」留口子)
  useEffect(() => {
    if (!symbol) return
    chartSession.setSymbol(symbol)
    // 换股后旧的定位指令、日期序列、信号事件都属于上一只票 —— 不清的话新图会先
    // 跳到上一只票那天, 时间轴也会先闪一下旧数据。
    chartFocus.consume()
    chartBars.set([])
    chartSignals.set([])
  }, [symbol])

  // ── 触发上下文 ────────────────────────────────────────────
  const stateCtx = (location.state as { trigger?: TriggerContext } | null)?.trigger ?? null
  const abnormal = useQuery({
    queryKey: QK.abnormalOverview(0.5, 300),
    queryFn: () => api.abnormalOverview(0.5, 300),
    enabled: !!symbol && stateCtx === null,
    staleTime: 60_000,
  })
  const abRow = useMemo(() => {
    const rows = (abnormal.data as unknown as { rows?: Record<string, unknown>[] } | undefined)?.rows
    if (!rows) return null
    return rows.find(r => r.symbol === symbol) ?? null
  }, [abnormal.data, symbol])

  const ctx: TriggerContext | null = useMemo(() => {
    if (stateCtx) return stateCtx
    if (!abRow) return null
    return {
      kind: 'abnormal',
      label: `异动 · ${String(abRow.status ?? '关注')}`,
      ts: (abRow.ts as number | string | undefined) ?? null,
      price: (abRow.price as number | null | undefined) ?? null,
      changePct: (abRow.change_pct as number | null | undefined) ?? null,
      message: (abRow.message as string | undefined) ?? '',
      signals: (abRow.signals as string[] | undefined) ?? [],
      backTo: '/abnormal',
    }
  }, [stateCtx, abRow])

  // ── 自选 ──────────────────────────────────────────────────
  const watchlist = useQuery({ queryKey: QK.watchlist, queryFn: () => api.watchlistList() })
  const inWatchlist = useMemo(
    () => (watchlist.data?.symbols ?? []).some(s => s.symbol === symbol),
    [watchlist.data, symbol],
  )
  const toggleWatchlist = useMutation({
    mutationFn: (action: 'add' | 'remove') =>
      action === 'add' ? api.watchlistAdd(symbol) : api.watchlistRemove(symbol),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.watchlist })
    },
  })

  // ── 左栏轨道 / [ ] 换股序列 ────────────────────────────────
  const { recent } = useRecentStocks({ symbol, name: quote.raw?.name ?? undefined })
  const watchItems = useMemo<RailItem[]>(
    () => (watchlist.data?.symbols ?? []).map(s => ({ symbol: s.symbol, name: s.name ?? null })),
    [watchlist.data],
  )
  const navSeq = useMemo(() => {
    const seen = new Set<string>()
    const out: string[] = []
    for (const it of [...watchItems, ...recent]) {
      if (seen.has(it.symbol)) continue
      seen.add(it.symbol)
      out.push(it.symbol)
    }
    return out
  }, [watchItems, recent])

  const gotoSymbol = useCallback((next: string) => {
    if (!next || next === symbol) return
    navigate(`/stock/${next}`, { state: location.state ?? undefined })
  }, [location.state, navigate, symbol])

  const stepSymbol = useCallback((delta: number) => {
    if (navSeq.length === 0) return
    const idx = navSeq.indexOf(symbol)
    if (idx < 0) { gotoSymbol(navSeq[0]); return }
    gotoSymbol(navSeq[(idx + delta + navSeq.length) % navSeq.length])
  }, [gotoSymbol, navSeq, symbol])

  const handleRefresh = () => {
    qc.invalidateQueries({ queryKey: ['kline', symbol] })
    qc.invalidateQueries({ queryKey: ['depth', symbol] })
  }

  const handleLocatePrice = (price: number) => {
    setPriceLines([{ value: price, color: '#F79009', label: '触发价' }])
  }

  const handleToggleKLine = useCallback(() => {
    const next: ChartRendererId = renderer === 'klinecharts' ? 'echarts' : 'klinecharts'
    setRenderer(next)
    setKLineProFlag(next === 'klinecharts')
    // 目标内核不支持当前周期时退化到最近可用档位(ECharts 无 1m -> 5m)。
    // 否则切过去会发现档位凭空消失 —— 这也是「割裂」的一种表现。
    const current = chartSession.getState().period
    const fp = fallbackPeriod(next, current)
    if (fp !== current) chartSession.setPeriod(fp)
  }, [renderer])

  // ── P2 键盘优先 ────────────────────────────────────────────
  useEffect(() => {
    if (!symbol) return
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setPaletteOpen(true)
        return
      }
      if (e.ctrlKey || e.metaKey || e.altKey) return
      if (paletteOpen || helpOpen || showMonitor) {
        if (e.key === 'Escape') {
          e.preventDefault()
          setPaletteOpen(false)
          setHelpOpen(false)
          setShowMonitor(false)
          // 关闭浮层后把焦点从 input 移走，否则后续键盘（如 '?'）会被输入框吞掉
          const active = document.activeElement as HTMLElement | null
          if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.isContentEditable)) active.blur()
        }
        return
      }
      switch (e.key) {
        case '/':
          e.preventDefault(); setPaletteOpen(true); break
        case '?':
          e.preventDefault(); setHelpOpen(true); break
        case 'c':
          chartSession.setOverlay('chan', !chanOn); break
        case 's':
          chartSession.setOverlay('structure', !structOn); break
        case 'x':
          chartSession.setOverlay('signals', !signalsOn); break
        case 'a':
          chartSession.setOverlay('alerts', !alertsOn); break
        case 't':
          chartSession.setOverlay('trades', !tradesOn); break
        case 'e':
          toggleTimeline(!timelineOn); break
        case 'r':
          setRailOpen(v => !v); break
        case 'p':
          setDepthOpen(v => !v); break
        case 'g':
          handleToggleKLine(); break
        case '[':
          stepSymbol(-1); break
        case ']':
          stepSymbol(1); break
        case '1':
          chartSession.setPeriod('day'); break
        case '2':
          chartSession.setPeriod('week'); break
        case '3':
          chartSession.setPeriod('month'); break
        case 'Escape':
          // 有浮层面板开着时让路: 用户按 Esc 是想关面板, 不是想离开当前标的。
          // (面板自己接管 Esc 关闭, 见 IndicatorManager 的 data-overlay-panel)
          if (document.querySelector('[data-overlay-panel]')) break
          e.preventDefault(); navigate(-1); break
        default:
          break
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [alertsOn, chanOn, handleToggleKLine, helpOpen, navigate, paletteOpen, showMonitor, signalsOn, stepSymbol, structOn, symbol, timelineOn, toggleTimeline, tradesOn])

  if (!symbol) {
    return (
      <div className="grid h-full place-items-center text-sm text-muted">
        缺少股票代码
      </div>
    )
  }

  return (
    <div className="relative flex h-full min-h-0 flex-col">
      <TerminalHeader
        symbol={symbol}
        name={quote.raw?.name ?? undefined}
        quote={quote}
        inWatchlist={inWatchlist}
        onToggleWatchlist={() => toggleWatchlist.mutate(inWatchlist ? 'remove' : 'add')}
        watchlistPending={toggleWatchlist.isPending}
        onMonitor={() => setShowMonitor(true)}
        onRefresh={handleRefresh}
        onClose={() => navigate(-1)}
      />

      <ContextRibbon ctx={ctx} onLocatePrice={handleLocatePrice} />

      <div className="flex min-h-0 flex-1 gap-3 px-3 pb-3 pt-2">
        {railDocked && (
          <aside className="w-[190px] shrink-0">
            <StockRail watchlist={watchItems} recent={recent} current={symbol} onSelect={gotoSymbol} className="h-full" />
          </aside>
        )}

        <main className="flex min-w-0 flex-1 flex-col">
          <div className="flex shrink-0 flex-wrap items-center gap-x-2 gap-y-1 pb-1.5">
            {!railDocked && !railDrawer && (
              <DrawerButton side="left" label="股票" onClick={() => setRailOpen(true)} />
            )}
            <RangeBar value={dateRange} onChange={setDateRange} />
            <div className="ml-auto flex items-center gap-1.5">
              {/* 周期按钮已下沉到各自内核的工具条(共用 lib/klinePeriod 的档位表),
                  这里不再重复渲染一组 —— 同屏两组周期控件本身就是割裂观感。 */}
              {/* 工作区模板: 一键落到一组预设(会话级, 切内核不丢) */}
              <select
                value=""
                title="工作区模板: 一键切换周期 / 复权 / 叠加层组合"
                onChange={e => {
                  const id = e.target.value
                  if (!id) return
                  applyWorkspace(id)
                  // 模板想要的档位当前渲染器不支持时(ECharts 无 1m)兜底退化
                  const cur = chartSession.getState().period
                  const fp = fallbackPeriod(renderer, cur)
                  if (fp !== cur) chartSession.setPeriod(fp)
                }}
                className="h-6 rounded border border-border/70 bg-elevated px-1 text-[11px] text-muted transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent"
              >
                <option value="">工作区…</option>
                {WORKSPACE_PRESETS.map(w => (
                  <option key={w.id} value={w.id} title={w.hint}>{w.label}</option>
                ))}
              </select>
              {/* 叠加层开关: 按渲染器能力显隐, 不按内核名分叉。
                  这一组是终端层唯一入口 —— 内核工具条在 hideOverlayToggles 下不再重复渲染。 */}
              {caps.overlays.structure && (
                <OverlayToggle
                  active={structOn}
                  label="定量结构"
                  title="主图定量结构: EMA25/89 双轨 + 九转 (s)"
                  onClick={() => chartSession.setOverlay('structure', !structOn)}
                />
              )}
              {caps.overlays.chan && (
                <OverlayToggle
                  active={chanOn}
                  label="缠论"
                  title="缠论笔 / 中枢 / 买卖点 (c)"
                  onClick={() => chartSession.setOverlay('chan', !chanOn)}
                />
              )}
              {caps.overlays.chips && (
                <OverlayToggle
                  active={chipsOn}
                  label="筹码"
                  title="筹码分布(成本分布): 右侧横条, 红=获利盘 / 绿=套牢盘"
                  onClick={() => chartSession.setOverlay('chips', !chipsOn)}
                />
              )}
              {caps.overlays.signals && (
                <OverlayToggle
                  active={signalsOn}
                  label="信号"
                  title="策略信号标记: 买=红下三角 / 卖=绿上三角 / 双向=蓝, 仅日线档 (x)"
                  onClick={() => chartSession.setOverlay('signals', !signalsOn)}
                />
              )}
              {caps.overlays.alerts && (
                <OverlayToggle
                  active={alertsOn}
                  label="触发"
                  title="监控触发记录: 近 7 天该股的告警, 画在 K 线上方(红=严重/橙=警告/蓝=提示), 仅日线档 (a)"
                  onClick={() => chartSession.setOverlay('alerts', !alertsOn)}
                />
              )}
              {caps.overlays.trades && (
                <OverlayToggle
                  active={tradesOn}
                  label="回测"
                  title="最近一次策略回测的买卖点: 买=红下三角 / 卖=绿上三角(带盈亏%), 仅日线档 (t)"
                  onClick={() => chartSession.setOverlay('trades', !tradesOn)}
                />
              )}
              {caps.focus && (
                <OverlayToggle
                  active={timelineOn}
                  label="时间轴"
                  title="事件时间轴: 图下方按日期排开信号 / 触发 / 买卖点, 点一下视口跳过去, 仅日线档 (e)"
                  onClick={() => toggleTimeline(!timelineOn)}
                />
              )}
              <button
                onClick={handleToggleKLine}
                title={useKLine ? '切回 ECharts 内核 (g)' : '试用 KLineChart 内核 (g)'}
                className={cn(
                  'h-6 rounded border px-2 text-[11px] font-mono transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent',
                  useKLine ? 'border-amber-500/30 bg-amber-500/20 text-amber-400' : 'border-transparent text-muted hover:bg-elevated',
                )}
              >
                {useKLine ? 'KLinePro' : 'ECharts'}
              </button>
              <button
                onClick={() => setHelpOpen(true)}
                title="键盘快捷键 (?)"
                className="h-6 rounded border border-transparent px-1.5 text-[11px] font-mono text-muted transition-colors hover:bg-elevated hover:text-foreground"
              >
                ?
              </button>
              {!depthDocked && !depthDrawer && (
                <DrawerButton side="right" label="盘口" onClick={() => setDepthOpen(true)} />
              )}
            </div>
          </div>
          <div ref={boxRef} className="min-h-0 flex-1">
            {useKLine ? (
              <KLinePro
                symbol={symbol}
                dateRange={dateRange}
                period={period}
                onPeriodChange={chartSession.setPeriod}
                adjust={adjust}
                onAdjustChange={chartSession.setAdjust}
                structureEnabled={structOn}
                onStructureChange={v => chartSession.setOverlay('structure', v)}
                chanEnabled={chanOn}
                chipsEnabled={chipsOn}
                onChipsChange={v => chartSession.setOverlay('chips', v)}
                signalsEnabled={signalsOn}
                onSignalsChange={v => chartSession.setOverlay('signals', v)}
                eventMarks={eventMarks}
                hideOverlayToggles
                priceLines={priceLines}
                refetchIntervalMs={refetchMs}
              />
            ) : (
              <StockPanel
                symbol={symbol}
                height={chartHeight}
                dateRange={dateRange}
                priceLines={priceLines}
                chanOverlay={chanOn}
                period={period}
                onPeriodChange={chartSession.setPeriod}
                adjust={adjust}
                onAdjustChange={chartSession.setAdjust}
                structureOverlay={structOn}
                onStructureChange={v => chartSession.setOverlay('structure', v)}
                signalsEnabled={signalsOn}
                eventMarks={eventMarks}
                hideOverlayToggles
                refetchIntervalMs={refetchMs}
                inWatchlist={inWatchlist}
                onAddToWatchlist={() => toggleWatchlist.mutate('add')}
                onRemoveFromWatchlist={() => toggleWatchlist.mutate('remove')}
                watchlistPending={toggleWatchlist.isPending}
                liveQuote={quote}
              />
            )}
          </div>
          {/* 事件时间轴: 只在日线档渲染 —— 告警与买卖点都是日粒度(周月线取不到),
              signal_* 列也被聚合丢掉了, 非日线档渲染出来只会是一条空轨道。 */}
          {timelineOn && period === 'day' && (
            <EventTimeline events={timelineEvents} dates={barDates} />
          )}
        </main>

        {depthDocked && (
          <aside className="w-[190px] shrink-0 overflow-hidden rounded-card border border-border bg-surface">
            <DepthPanel symbol={symbol} refetchIntervalMs={refetchMs} />
          </aside>
        )}
      </div>

      {/* 抽屉形态：窄屏时左右两栏浮在图表之上 */}
      {railDrawer && (
        <div className="absolute inset-y-0 left-0 z-20 flex w-[220px] p-3">
          <div className="absolute inset-0 bg-black/40" onClick={() => setRailOpen(false)} />
          <div className="relative h-full w-full">
            <StockRail
              watchlist={watchItems}
              recent={recent}
              current={symbol}
              onSelect={s => { gotoSymbol(s); setRailOpen(false) }}
              className="h-full"
            />
          </div>
        </div>
      )}
      {depthDrawer && (
        <div className="absolute inset-y-0 right-0 z-20 flex w-[220px] p-3">
          <div className="absolute inset-0 bg-black/40" onClick={() => setDepthOpen(false)} />
          <div className="relative h-full w-full overflow-hidden rounded-card border border-border bg-surface">
            <DepthPanel symbol={symbol} refetchIntervalMs={refetchMs} />
          </div>
        </div>
      )}

      {paletteOpen && (
        <CommandPalette
          current={symbol}
          recent={recent}
          onPick={s => { setPaletteOpen(false); gotoSymbol(s) }}
          onClose={() => setPaletteOpen(false)}
        />
      )}

      {helpOpen && <ShortcutHelp onClose={() => setHelpOpen(false)} />}

      {showMonitor && (
        <div
          className="absolute inset-0 z-20 flex items-start justify-center overflow-auto bg-black/40 p-4"
          onClick={() => setShowMonitor(false)}
        >
          <div className="mt-8 w-full max-w-2xl" onClick={e => e.stopPropagation()}>
            <RuleEditor
              rule={null}
              simple
              preset={{ scope: 'symbols', symbols: [symbol], type: 'signal', logic: 'or' }}
              onClose={() => setShowMonitor(false)}
              onSaved={() => setShowMonitor(false)}
            />
          </div>
        </div>
      )}
    </div>
  )
}
