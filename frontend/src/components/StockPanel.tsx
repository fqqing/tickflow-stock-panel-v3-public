import { useEffect, useState, useCallback, useRef, useMemo } from 'react'
import { X } from 'lucide-react'
import { type FinancialMetricRecord } from '@/lib/api'
import { StockInfoBar } from '@/components/StockInfoBar'
import { KLinePro, type KLineDataSnapshot } from '@/components/kline/KLinePro'
import { getDefaultRange } from '@/lib/dateRange'
import type { KLineAdjust, KLinePeriod } from '@/lib/klinePeriod'
import type { ChartEventPoint } from '@/lib/chart-events'
import { StockIntradayChart } from '@/components/StockIntradayChart'
import { useFinancialMetrics } from '@/lib/useFinancials'
import { useCapabilities } from '@/lib/useSharedQueries'
import type { UnifiedQuote } from '@/lib/useQuote'
import type { ChartPriceLine, ChartRange } from '@/lib/chart-primitives'
import {
  loadInfoFields,
  saveInfoFields,
  buildInfoExtColumnsParam,
  type ColumnConfig,
} from '@/lib/stock-info-fields'

interface Props {
  symbol: string
  height?: number
  showIntraday?: boolean
  className?: string
  /** 当用户点击蜡烛选中日期时回调（用于外部自动开启分时图）。 */
  onSelectDate?: (date: string) => void
  /** 外部传入的日期范围 */
  dateRange?: { start: string; end: string }
  /** 横向日期区间高亮(回测持仓区间等) */
  ranges?: ChartRange[]
  /** 价位水平线(监控触发价 / 回测买卖价等) */
  priceLines?: ChartPriceLine[]
  /** 缠论叠加开关（受控）。传 undefined = 不启用缠论, 也不显示「缠论」按钮 */
  chanOverlay?: boolean
  onToggleChan?: () => void
  /** K 线周期（受控，透传给 KLinePro）。 */
  period?: KLinePeriod
  onPeriodChange?: (p: KLinePeriod) => void
  /** 主图定量结构开关（受控，透传）。 */
  structureOverlay?: boolean
  onStructureChange?: (v: boolean) => void
  /** 策略信号标记开关（受控，透传）。 */
  signalsEnabled?: boolean
  /** 外部事件标记(监控触发 / 回测买卖点) */
  eventMarks?: ChartEventPoint[]
  /** true = 隐藏图内叠加层开关(终端层已提供统一入口) */
  hideOverlayToggles?: boolean
  /** 复权方式（受控，透传）。 */
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
  /** 分时图/分钟K 自动刷新间隔(ms)。undefined = 不轮询。 */
  refetchIntervalMs?: number
  /** 只渲染信息条, 隐藏图表 (用于分时 tab 共享信息条) */
  infoBarOnly?: boolean
  /**
   * 实时快照（价格单一源），透传给 StockInfoBar。
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
  ranges,
  priceLines,
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
  // KLinePro 的数据快照(信息条 + 分时联动日期序列)
  const [snapshot, setSnapshot] = useState<KLineDataSnapshot | null>(null)
  // 信息条指标配置提升到此层：同时供 StockInfoBar 渲染与 KLinePro 请求 ext 数据
  const [fields, setFields] = useState<ColumnConfig[]>(loadInfoFields)
  const extColumns = useMemo(() => buildInfoExtColumnsParam(fields), [fields])

  const handleFieldsChange = useCallback((next: ColumnConfig[]) => {
    setFields(next)
    saveInfoFields(next)
  }, [])

  // 财务指标：仅当信息条配置含可见的财务字段且用户具备财务数据能力 (financial) 时才请求
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

  const handleDataChange = useCallback((snap: KLineDataSnapshot) => {
    setSnapshot(snap)
  }, [])

  const dates = snapshot?.dates ?? []
  const rawRows = snapshot?.rawRows ?? []
  const stockInfo = snapshot?.stockInfo
  const name = snapshot?.name

  const currentPeriod = period ?? 'day'

  // symbol 变化时重置分时相关状态，避免切股后残留旧日期。
  // 注意：必须跳过首次挂载——重开弹窗时 kline 命中 react-query 缓存，
  // 子组件 onDataChange effect（先于父 effect 执行）会把 snapshot 置为有效数据，
  // 若此处再无条件清空，会把刚加载的数据抹掉，导致信息条整行消失。
  const prevSymbol = useRef<string | null>(symbol)
  useEffect(() => {
    if (prevSymbol.current === symbol) return
    prevSymbol.current = symbol
    setSelectedDate(null)
    setLinkedPrice(null)
    setSnapshot(null)
  }, [symbol])

  // 当分时开启、无选中日期时，自动选中最新日期
  useEffect(() => {
    if (showIntraday && !selectedDate && dates.length > 0) {
      setSelectedDate(dates[dates.length - 1])
    }
  }, [showIntraday, selectedDate, dates])

  const selectedIdx = selectedDate ? dates.indexOf(selectedDate) : -1
  const prevClose = selectedIdx > 0
    ? Number(rawRows[selectedIdx - 1]?.close)
    : rawRows.length >= 2
      ? Number(rawRows[rawRows.length - 2]?.close)
      : undefined
  const currentPrice = rawRows.length > 0 ? Number(rawRows[rawRows.length - 1].close) : undefined

  // 分时图 hover 的联动价并入 KLinePro 的价位线(临时参考线, 与监控线同槽位)
  const effectivePriceLines = useMemo<ChartPriceLine[]>(() => {
    if (linkedPrice == null) return priceLines ?? []
    return [...(priceLines ?? []), { value: linkedPrice, color: '#F79009', label: '联动' }]
  }, [linkedPrice, priceLines])

  if (!symbol) return null

  // 财务指标最新一期（metrics 按 period_end 排序，取首项）
  const financialMetrics: FinancialMetricRecord | undefined = financials.data?.data?.[0]

  return (
    <div className={className}>
      <StockInfoBar
        symbol={symbol}
        name={name}
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
        <div className="flex-1 min-w-0">
          {/* 缠论入口: KLinePro 不渲染缠论按钮(纯受控), 由这里提供 */}
          {chanOverlay !== undefined && onToggleChan !== undefined && (
            <div className="flex items-center gap-1.5 px-1 pb-0.5">
              <button
                onClick={onToggleChan}
                disabled={currentPeriod !== 'day'}
                title={currentPeriod !== 'day'
                  ? '缠论基于日线笔/中枢, 仅在日K周期下可用'
                  : chanOverlay ? '隐藏缠论笔/中枢/买卖点' : '显示缠论笔/中枢/买卖点'}
                className={`px-2 py-0.5 rounded text-[10px] font-mono transition-colors ${
                  currentPeriod !== 'day'
                    ? 'bg-elevated text-muted/40 cursor-not-allowed'
                    : chanOverlay
                      ? 'text-accent bg-accent/15 cursor-pointer'
                      : 'bg-elevated text-muted hover:text-secondary cursor-pointer'
                }`}
              >
                缠论
              </button>
            </div>
          )}
          <div style={{ height }}>
            <KLinePro
              symbol={symbol}
              dateRange={dateRange}
              period={period}
              onPeriodChange={onPeriodChange}
              adjust={adjust}
              onAdjustChange={onAdjustChange}
              structureEnabled={structureOverlay}
              onStructureChange={onStructureChange}
              chanEnabled={chanOverlay === true && !infoBarOnly}
              priceLines={effectivePriceLines}
              ranges={ranges}
              signalsEnabled={signalsEnabled}
              eventMarks={eventMarks}
              hideOverlayToggles={hideOverlayToggles}
              extColumns={extColumns}
              onDataChange={handleDataChange}
              onDateClick={handleDateClick}
              onPriceDoubleClick={onPriceDoubleClick}
              refetchIntervalMs={refetchIntervalMs}
            />
          </div>
        </div>

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
              currentPrice={currentPrice}
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
