import { useEffect, useState, useCallback, useRef, useMemo } from 'react'
import { X } from 'lucide-react'
import { type KlineRow, type FinancialMetricRecord } from '@/lib/api'
import { StockInfoBar } from '@/components/StockInfoBar'
import { StockDailyKChart, getDefaultRange, type KLinePeriod, type StockDailyKChartResult } from '@/components/StockDailyKChart'
import type { KLineAdjust } from '@/lib/klinePeriod'
import type { ChartEventPoint } from '@/lib/chart-events'
import { StockIntradayChart } from '@/components/StockIntradayChart'
import { useFinancialMetrics } from '@/lib/useFinancials'
import { useCapabilities } from '@/lib/useSharedQueries'
import { useChanOverlay } from '@/lib/useChanOverlay'
import type { UnifiedQuote } from '@/lib/useQuote'
import type { ChartMarker, ChartPriceLine, ChartRange } from '@/components/EChartsCandlestick'
import {
  loadInfoFields,
  saveInfoFields,
  buildInfoExtColumnsParam,
  type ColumnConfig,
} from '@/lib/stock-info-fields'

/**
 * 缠论开启时图表的初始可视根数。
 * 120 根在 500px 宽的图里约 4px/根，既能看清笔的折点，又能把最近 1~2 个中枢纳入视野。
 */
const CHAN_VISIBLE_BARS = 120

interface Props {
  symbol: string
  height?: number
  showIntraday?: boolean
  className?: string
  /** 当用户点击蜡烛选中日期时回调（用于外部自动开启分时图）。 */
  onSelectDate?: (date: string) => void
  /** 外部传入的日期范围 */
  dateRange?: { start: string; end: string }
  markers?: ChartMarker[]
  ranges?: ChartRange[]
  priceLines?: ChartPriceLine[]
  showLimitMarkers?: boolean
  showMarkerToggle?: boolean
  /** 缠论叠加开关（受控）。传 undefined = 不启用缠论, 也不显示「缠论」按钮 */
  chanOverlay?: boolean
  onToggleChan?: () => void
  /** K 线周期（受控，透传给 StockDailyKChart）。终端层持有，供键盘 1/2/3 跨内核生效 */
  period?: KLinePeriod
  onPeriodChange?: (p: KLinePeriod) => void
  /** 主图定量结构开关（受控，透传）。终端层持有，与 KLinePro 共用一份状态 */
  structureOverlay?: boolean
  onStructureChange?: (v: boolean) => void
  /** 策略信号标记开关（受控，透传）。与 KLinePro 共用会话里的同一份状态 */
  signalsEnabled?: boolean
  /** 外部事件标记(监控触发 / 回测买卖点), 透传给日K图, 两内核同口径 */
  eventMarks?: ChartEventPoint[]
  /** true = 隐藏图内叠加层开关(终端层已提供统一入口, 避免同屏两组同名按钮) */
  hideOverlayToggles?: boolean
  /** 复权方式（受控，透传）。终端层持有，与 KLinePro 共用一份状态 */
  adjust?: KLineAdjust
  onAdjustChange?: (a: KLineAdjust) => void
  /** 加监控回调 (传入后信息条显示 RadioTower 图标) */
  onMonitor?: () => void
  onPriceDoubleClick?: (price: number, currentPrice: number) => void
  /** 自选操作（传入后信息条显示 Star 图标） */
  inWatchlist?: boolean
  onAddToWatchlist?: (groupId: string | null) => void
  onRemoveFromWatchlist?: () => void
  watchlistPending?: boolean
  /** 分时图自动刷新间隔(ms)。undefined = 不轮询。个股对话框盘中实时刷新时传入。 */
  refetchIntervalMs?: number
  /** 只渲染信息条, 隐藏图表 (用于分时 tab 共享信息条) */
  infoBarOnly?: boolean
  /**
   * 实时快照（价格单一源），透传给 StockInfoBar。
   * 传入后信息条主价格与顶栏/盘口同源, 不传则沿用日 K 最后一根(向后兼容)。
   */
  liveQuote?: UnifiedQuote | null
}

export { getDefaultRange }

export function StockPanel({
  symbol,
  height = 520,
  showIntraday = true,
  className,
  onSelectDate,
  dateRange: externalDateRange,
  markers,
  ranges,
  priceLines,
  showLimitMarkers = true,
  showMarkerToggle = true,
  chanOverlay,
  onToggleChan,
  period,
  onPeriodChange,
  structureOverlay,
  onStructureChange,
  signalsEnabled,
  eventMarks,
  hideOverlayToggles,
  adjust,
  onAdjustChange,
  onMonitor,
  onPriceDoubleClick,
  inWatchlist,
  onAddToWatchlist,
  onRemoveFromWatchlist,
  watchlistPending,
  refetchIntervalMs,
  infoBarOnly = false,
  liveQuote,
}: Props) {
  const [linkedPrice, setLinkedPrice] = useState<number | null>(null)
  const [selectedDate, setSelectedDate] = useState<string | null>(null)
  const [intradayDismissed, setIntradayDismissed] = useState(false)
  const [dailyResult, setDailyResult] = useState<StockDailyKChartResult | null>(null)
  // 信息条指标配置提升到此层：同时供 StockInfoBar 渲染与 StockDailyKChart 请求 ext 数据
  const [fields, setFields] = useState<ColumnConfig[]>(loadInfoFields)
  const extColumns = useMemo(() => buildInfoExtColumnsParam(fields), [fields])

  const handleFieldsChange = useCallback((next: ColumnConfig[]) => {
    setFields(next)
    saveInfoFields(next)
  }, [])

  // 财务指标：仅当信息条配置含可见的财务字段且用户具备财务数据能力 (financial) 时才请求
  // 无能力时跳过请求, 避免后端抛 CapabilityDenied (403) 导致 free/starter 档弹错误提示
  const { data: caps } = useCapabilities()
  const hasFinancialCap = !!caps?.capabilities?.['financial']
  const hasFinanceField = useMemo(
    () => fields.some(f => f.visible && f.source.type === 'builtin'
      && ['eps', 'bps', 'roe', 'pe_ttm', 'pb', 'gross_margin', 'net_margin', 'debt_ratio', 'revenue_yoy', 'net_income_yoy'].includes(f.source.key)),
    [fields],
  )
  const financials = useFinancialMetrics(hasFinanceField && hasFinancialCap ? symbol : undefined)

  const dateRange = externalDateRange ?? getDefaultRange()

  const handleDateClick = useCallback((date: string) => {
    setSelectedDate(date)
    setIntradayDismissed(false)
    onSelectDate?.(date)
  }, [onSelectDate])

  const rows = dailyResult?.rows ?? []
  const stockInfo = dailyResult?.stockInfo
  const rawRows: KlineRow[] = dailyResult?.rawRows ?? []

  // ── 缠论叠加 ──────────────────────────────────────────────────
  // 图表 x 轴日期序列：由日K结果派生，供缠论叠加层裁剪到当前显示区间
  const chartDates = useMemo(() => (dailyResult?.rows ?? []).map(r => r.date), [dailyResult])
  const chanActive = chanOverlay === true && !infoBarOnly
  const chanLayers = useChanOverlay(symbol, chartDates, chanActive)
  // 合并外部标注与缠论标注；用 useMemo 保住引用，避免 ECharts 每次渲染都全量 setOption
  const mergedMarkers = useMemo(
    () => (chanLayers.markers.length > 0 ? [...(markers ?? []), ...chanLayers.markers] : markers),
    [markers, chanLayers.markers],
  )
  const mergedRanges = useMemo(
    () => (chanLayers.ranges.length > 0 ? [...(ranges ?? []), ...chanLayers.ranges] : ranges),
    [ranges, chanLayers.ranges],
  )
  // 缠论开启时放宽初始可视根数：中枢一定在买点之前（三买更是如此），
  // 默认 40~60 根只会看到箭头看不到它所依附的中枢，等于信息残缺。
  const baseVisibleBars = chanActive ? CHAN_VISIBLE_BARS : showIntraday ? 40 : 60

  // symbol 变化时重置分时相关状态，避免切股后残留旧日期。
  // 注意：必须跳过首次挂载——重开弹窗时 kline 命中 react-query 缓存，
  // 子组件 onDataChange effect（先于父 effect 执行）会把 dailyResult 置为有效数据，
  // 若此处再无条件清空，会把刚加载的数据抹掉，导致信息条整行消失。
  const prevSymbol = useRef<string | null>(symbol)
  useEffect(() => {
    if (prevSymbol.current === symbol) return
    prevSymbol.current = symbol
    setSelectedDate(null)
    setLinkedPrice(null)
    setDailyResult(null)
  }, [symbol])

  // 当分时开启、无选中日期时，自动选中最新日期
  useEffect(() => {
    if (showIntraday && !selectedDate && rows.length > 0) {
      setSelectedDate(rows[rows.length - 1].date)
    }
  }, [showIntraday, selectedDate, rows])

  const selectedIdx = selectedDate ? rows.findIndex(r => r.date === selectedDate) : -1
  const prevClose = selectedIdx > 0
    ? rows[selectedIdx - 1].close
    : rows.length >= 2
      ? rows[rows.length - 2].close
      : undefined
  if (!symbol) return null

  // 财务指标最新一期（metrics 按 period_end 排序，取首项）
  const financialMetrics: FinancialMetricRecord | undefined = financials.data?.data?.[0]

  return (
    <div className={className}>
      <StockInfoBar
        symbol={symbol}
        name={dailyResult?.name}
        stockInfo={stockInfo}
        rows={rawRows}
        fields={fields}
        onFieldsChange={handleFieldsChange}
        financialMetrics={financialMetrics}
        liveQuote={liveQuote}
        onMonitor={onMonitor}
        inWatchlist={inWatchlist}
        onAddToWatchlist={onAddToWatchlist}
        onRemoveFromWatchlist={onRemoveFromWatchlist}
        watchlistPending={watchlistPending}
      />

      {infoBarOnly ? null : (
      <div className="flex gap-3 items-start">
        <StockDailyKChart
          symbol={symbol}
          height={height}
          className="flex-1 min-w-0"
          dateRange={dateRange}
          markers={mergedMarkers}
          ranges={mergedRanges}
          priceLines={priceLines}
          polylines={chanLayers.polylines}
          showLimitMarkers={showLimitMarkers}
          showMarkerToggle={showMarkerToggle}
          chanEnabled={chanOverlay === undefined ? undefined : chanOverlay === true}
          onToggleChan={onToggleChan}
          period={period}
          onPeriodChange={onPeriodChange}
          structureOverlay={structureOverlay}
          onStructureChange={onStructureChange}
          signalsEnabled={signalsEnabled}
          eventMarks={eventMarks}
          hideOverlayToggles={hideOverlayToggles}
          adjust={adjust}
          onAdjustChange={onAdjustChange}
          linkedPrice={linkedPrice}
          onDateClick={handleDateClick}
          onPriceDoubleClick={onPriceDoubleClick}
          onDataChange={setDailyResult}
          visibleBars={baseVisibleBars}
          extColumns={extColumns}
        />

        {showIntraday && selectedDate && !intradayDismissed && (
          <div className="relative flex-1 min-w-0 border-l border-border pl-3">
            <button
              onClick={() => setIntradayDismissed(true)}
              className="absolute -left-1.5 -top-1.5 z-10 flex h-5 w-5 items-center justify-center rounded-full border border-border bg-surface text-muted shadow-sm transition-colors hover:text-foreground hover:bg-elevated"
              title="收起分时图"
              aria-label="收起分时图"
            >
              <X className="h-3 w-3" />
            </button>
            <StockIntradayChart
              symbol={symbol}
              date={selectedDate}
              height={height}
              prevClose={prevClose}
              onPriceHover={setLinkedPrice}
              onPriceDoubleClick={onPriceDoubleClick}
              currentPrice={rows[rows.length - 1]?.close}
              priceLines={priceLines}
              refetchIntervalMs={refetchIntervalMs}
            />
          </div>
        )}
      </div>
      )}
    </div>
  )
}
