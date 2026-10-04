import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, KLINE_CHART_FIELDS, klineChartFields, type KlineRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import type { ChartEventPoint } from '@/lib/chart-events'
import { storage } from '@/lib/storage'
import {
  ADJUST_OPTIONS,
  isMinutePeriod,
  periodTabTitle,
  periodTabsFor,
  type KLineAdjust,
  type KLinePeriod,
} from '@/lib/klinePeriod'
import {
  EChartsCandlestick,
  OVERLAY_INDICATORS,
  SUB_CHARTS,
  type ChartMarker,
  type ChartPolyline,
  type ChartPriceLine,
  type ChartRange,
  type OHLC,
  type StockInfo,
  type VolumeCompareConfig,
} from '@/components/EChartsCandlestick'

const SUB_INFO_H = 16
const SUB_GAP = 4
const MAX_DAYS = 2000
/** 分钟周期回看多少个交易日的 1 分钟数据(30 分钟档约得 8 根/日 -> 约 960 根) */
const MINUTE_LOOKBACK_DAYS = 120

/**
 * 周期 / 复权的类型与档位表已从 lib/klinePeriod 收口(两个内核共用一份)。
 * 这里原样重导出, 兼容既有 import 路径。
 */
export type { KLinePeriod, KLineAdjust }
export { isMinutePeriod }
/** 这两个主图叠加指标的开关放顶部工具条(与缠论同组), 不占底部副图指标栏 */
const TOP_TOOLBAR_OVERLAYS = ['tdragon', 'structure']
/** ECharts 不列 1m(单日 240 根, 渲染掉帧) —— 由 PERIOD_CAPABILITY 决定, 不在这里硬编码 */
const PERIOD_OPTIONS = periodTabsFor('echarts')
const DEFAULT_VOLUME_COMPARE: VolumeCompareConfig = { enabled: true, days: 1 }

/** 用户手绘线: 端点存 (date, price), 按 symbol 存 localStorage */
interface DrawLine { a: { date: string; price: number }; b: { date: string; price: number } }
const DRAW_COLOR = '#F59E0B'
const linesKey = (symbol: string) => `tickflow.kline.drawlines.${symbol}`
function loadLines(symbol: string): DrawLine[] {
  try {
    const raw = localStorage.getItem(linesKey(symbol))
    const arr = raw ? JSON.parse(raw) : []
    return Array.isArray(arr) ? arr.filter((l: DrawLine) => l?.a?.date && l?.b?.date) : []
  } catch {
    return []
  }
}
function saveLines(symbol: string, lines: DrawLine[]) {
  try {
    localStorage.setItem(linesKey(symbol), JSON.stringify(lines))
  } catch {
    return
  }
}
/**
 * 解密公式派生指标：蛟龙出海 + 资金动能 + 主图定量结构 + MACD 定量结构，
 * 由后端按需计算（公式预热依赖长历史，前端只负责展示）
 */
const CUSTOM_INDICATORS = 'trend_dragon,capital_momentum,structure,macd_structure'

function normalizeVolumeCompare(config: VolumeCompareConfig): VolumeCompareConfig {
  return {
    enabled: config.enabled !== false,
    days: Math.max(1, Math.min(20, Math.round(Number(config.days) || 1))),
  }
}

export interface StockDailyKChartResult {
  rows: OHLC[]
  rawRows: KlineRow[]
  stockInfo?: StockInfo
  name?: string
}

interface Props {
  symbol: string
  height?: number
  className?: string
  dateRange?: { start: string; end: string }
  markers?: ChartMarker[]
  ranges?: ChartRange[]
  priceLines?: ChartPriceLine[]
  /** 主图折线（缠论笔） */
  polylines?: ChartPolyline[]
  showLimitMarkers?: boolean
  showIndicatorControls?: boolean
  showMarkerToggle?: boolean
  /**
   * 缠论叠加开关（受控）。传 undefined = 不渲染「缠论」按钮、也不画缠论。
   * 数据由上层（StockPanel）注入到 markers/ranges/polylines。
   */
  chanEnabled?: boolean
  onToggleChan?: () => void
  /**
   * K 线周期（受控）。不传则退回组件内部状态（弹窗/预览等独立用法）。
   * 个股终端把它提升到终端层, 这样键盘 1/2/3 在 ECharts 与 KLinePro 两个内核下都能生效。
   */
  period?: KLinePeriod
  onPeriodChange?: (p: KLinePeriod) => void
  /**
   * 主图定量结构开关(受控, 可选)。终端层持有后与 KLinePro 共用一份状态,
   * 切内核不丢 —— 此前「结构信号只长在 ECharts 上」是割裂最痛的一处。
   * 不传则维持组件内部状态(默认关), 弹窗/预览等独立用法不受影响。
   */
  structureOverlay?: boolean
  onStructureChange?: (v: boolean) => void
  /**
   * 策略信号标记开关(受控, 可选)。与 KLinePro 侧同一套口径(共用中文名/配色)。
   * 打开会在 fields 里追加 signal_* 列(1000 根约 +300KB 未压缩), 默认关。
   * 只有日线档有意义 —— 周/月线是聚合结果, 分钟档没有这些列。
   */
  signalsEnabled?: boolean
  /** 外部事件标记(监控触发 / 回测买卖点), 与 KLinePro 侧共用同一份归约结果 */
  eventMarks?: ChartEventPoint[]
  /**
   * true = 隐藏图内的叠加层开关(主图定量结构 / 缠论)。
   * 终端层已提供统一入口时传 true —— 同屏两组同名按钮本身就是「割裂」观感,
   * 而且两处状态来源一旦不一致, 排查成本极高。
   */
  hideOverlayToggles?: boolean
  /**
   * 复权方式(受控, 可选)。终端层持有后与 KLinePro 共用一份口径, 切内核不丢。
   * 不传则维持组件内部状态(默认前复权)。
   */
  adjust?: KLineAdjust
  onAdjustChange?: (a: KLineAdjust) => void
  showMA?: boolean
  showInfoBar?: boolean
  visibleBars?: number
  linkedPrice?: number | null
  onDateClick?: (date: string) => void
  onPriceDoubleClick?: (price: number, currentPrice: number) => void
  onDataChange?: (result: StockDailyKChartResult) => void
  /** 扩展数据列参数（逗号分隔 config_id.field_name），透传给 klineDaily 接口 */
  extColumns?: string
}

function isValidRow(r: any): boolean {
  return r && r.date != null && r.open != null && r.close != null
}

/**
 * x 轴标签: 日/周/月是 YYYY-MM-DD; 分钟周期是完整时间戳, 必须保留到分钟,
 * 否则同一天的多根 K 会塌成同一个类目。
 */
function barLabel(d: unknown): string {
  const s = typeof d === 'string' ? d : String(d)
  return s.length > 10 ? s.slice(0, 16).replace('T', ' ') : s
}

/**
 * 只透出 signal_* 布尔列。
 *
 * ★ 事件时间轴(lib/chart-timeline.ts 的 signalRowsToTimeline)靠 `signal_` 前缀
 *   识别信号, 而 toOHLC 此前只挑固定字段 —— 于是 ECharts 内核下时间轴的「信号」
 *   恒为 0 条(KLinePro 直接用原始行, 不受影响)。带上信号列后, 两个内核的时间轴
 *   才是真的同源。
 *   只带 signal_* 而不整行展开, 是为了不让 ext / symbol 等列污染下游遍历。
 */
function pickSignalColumns(r: KlineRow): Record<string, boolean> {
  const out: Record<string, boolean> = {}
  for (const [k, v] of Object.entries(r)) {
    if (k.startsWith('signal_')) out[k] = v === true
  }
  return out
}

export function toOHLC(rows: KlineRow[]): OHLC[] {
  return rows
    .filter(isValidRow)
    .map(r => ({
      ...pickSignalColumns(r),
      date: barLabel(r.date),
      open: Number(r.open),
      high: Number(r.high),
      low: Number(r.low),
      close: Number(r.close),
      volume: Number(r.volume ?? 0),
      ma5: r.ma5 != null ? Number(r.ma5) : null,
      ma10: r.ma10 != null ? Number(r.ma10) : null,
      ma20: r.ma20 != null ? Number(r.ma20) : null,
      ma60: r.ma60 != null ? Number(r.ma60) : null,
      macd_dif: r.macd_dif != null ? Number(r.macd_dif) : null,
      macd_dea: r.macd_dea != null ? Number(r.macd_dea) : null,
      macd_hist: r.macd_hist != null ? Number(r.macd_hist) : null,
      rsi_6: r.rsi_6 != null ? Number(r.rsi_6) : null,
      rsi_14: r.rsi_14 != null ? Number(r.rsi_14) : null,
      rsi_24: r.rsi_24 != null ? Number(r.rsi_24) : null,
      kdj_k: r.kdj_k != null ? Number(r.kdj_k) : null,
      kdj_d: r.kdj_d != null ? Number(r.kdj_d) : null,
      kdj_j: r.kdj_j != null ? Number(r.kdj_j) : null,
      boll_upper: r.boll_upper != null ? Number(r.boll_upper) : null,
      boll_lower: r.boll_lower != null ? Number(r.boll_lower) : null,
      td_signal: r.td_signal === true,
      td_a3: r.td_a3 != null ? Number(r.td_a3) : null,
      cm_value: r.cm_value != null ? Number(r.cm_value) : null,
      st_dsg: r.st_dsg != null ? Number(r.st_dsg) : null,
      st_dxg: r.st_dxg != null ? Number(r.st_dxg) : null,
      st_csg: r.st_csg != null ? Number(r.st_csg) : null,
      st_cxg: r.st_cxg != null ? Number(r.st_cxg) : null,
      st_icon: Number(r.st_icon ?? 0),
      st_dn: Number(r.st_dn ?? 0),
      st_up: Number(r.st_up ?? 0),
      ms_diff: r.ms_diff != null ? Number(r.ms_diff) : null,
      ms_dea: r.ms_dea != null ? Number(r.ms_dea) : null,
      ms_hist: r.ms_hist != null ? Number(r.ms_hist) : null,
      ms_btext: Number(r.ms_btext ?? 0),
      ms_by: r.ms_by != null ? Number(r.ms_by) : null,
      ms_ttext: Number(r.ms_ttext ?? 0),
      ms_ty: r.ms_ty != null ? Number(r.ms_ty) : null,
    }))
}

function buildLimitUpMarkers(rows: KlineRow[]): ChartMarker[] {
  const markers: ChartMarker[] = []
  for (const r of rows) {
    const date = barLabel(r.date)
    if (r.signal_broken_limit_up) {
      markers.push({ date, kind: 'neutral', above: true, color: '#8B5CF6', label: '炸' })
    } else if (r.signal_limit_up) {
      const boards: number = r.consecutive_limit_ups ?? 1
      markers.push({ date, kind: 'buy', above: true, color: '#FACC15', label: boards <= 1 ? '板' : String(boards) })
    }
  }
  return markers
}

export function getDefaultRange(): { start: string; end: string } {
  const now = new Date()
  const end = now.toISOString().slice(0, 10)
  const s = new Date(now)
  s.setMonth(s.getMonth() - 6)
  const start = s.toISOString().slice(0, 10)
  return { start, end }
}

function rangeDays(range: { start: string; end: string }): number {
  const start = new Date(range.start)
  const end = new Date(range.end)
  return Math.min(Math.ceil((end.getTime() - start.getTime()) / 86400000) + 30, MAX_DAYS)
}

export function StockDailyKChart({
  symbol,
  height = 520,
  className,
  dateRange: externalDateRange,
  markers,
  ranges,
  priceLines,
  polylines,
  showLimitMarkers = true,
  showIndicatorControls = true,
  showMarkerToggle = true,
  chanEnabled,
  onToggleChan,
  period: externalPeriod,
  onPeriodChange,
  structureOverlay,
  onStructureChange,
  signalsEnabled = false,
  eventMarks,
  hideOverlayToggles = false,
  adjust: adjustProp,
  onAdjustChange,
  showMA = true,
  showInfoBar = true,
  visibleBars = 60,
  linkedPrice,
  onDateClick,
  onPriceDoubleClick,
  onDataChange,
  extColumns,
}: Props) {
  const [activeIndicators, setActiveIndicators] = useState<string[]>(['vol'])
  const [showMarkers, setShowMarkers] = useState(true)
  // K 线周期: 日/周/月。周月由后端按日 K 聚合, 并在聚合后重算指标
  // (周线 MA20 是 20 周均线, 不是日线 MA20 在周末那天的取值)。
  // 受控优先：终端层持有周期时, 图内按钮与键盘 1/2/3 走同一份状态。
  const [innerPeriod, setInnerPeriod] = useState<KLinePeriod>('day')
  const period = externalPeriod ?? innerPeriod
  const applyPeriod = useCallback((p: KLinePeriod) => {
    if (onPeriodChange) onPeriodChange(p)
    else setInnerPeriod(p)
  }, [onPeriodChange])
  // 复权: 受控优先(终端层持有), 不传则组件内部维护
  const [innerAdjust, setInnerAdjust] = useState<KLineAdjust>('qfq')
  const adjust = adjustProp ?? innerAdjust
  const applyAdjust = useCallback((a: KLineAdjust) => {
    if (onAdjustChange) onAdjustChange(a)
    else setInnerAdjust(a)
  }, [onAdjustChange])
  // 手绘趋势线: 非画线模式下不拦截鼠标事件, 画线模式在 zrender 上手动拖拽
  const [drawing, setDrawing] = useState(false)
  const [drawLines, setDrawLines] = useState<DrawLine[]>([])
  const [chartInst, setChartInst] = useState<any>(null)
  const [preview, setPreview] = useState<{ x1: number; y1: number; x2: number; y2: number } | null>(null)
  const dragRef = useRef<{ x: number; y: number; date: string; price: number } | null>(null)
  const drawingRef = useRef(drawing)
  drawingRef.current = drawing
  const rowsRef = useRef<OHLC[]>([])
  const [volumeCompare, setVolumeCompare] = useState<VolumeCompareConfig>(() =>
    normalizeVolumeCompare(storage.stockVolumeCompare.get(DEFAULT_VOLUME_COMPARE)),
  )
  const dateRange = externalDateRange ?? getDefaultRange()
  const days = useMemo(() => rangeDays(dateRange), [dateRange])

  const minutePeriod = isMinutePeriod(period)
  // 分钟周期走 /kline/minute-k (1 分钟 K 聚合); 日/周/月走 /kline/daily
  const klineMinute = useQuery({
    queryKey: QK.klineMinuteK(symbol, period, MINUTE_LOOKBACK_DAYS),
    queryFn: () => api.klineMinuteK(symbol, period, MINUTE_LOOKBACK_DAYS, KLINE_CHART_FIELDS),
    enabled: !!symbol && minutePeriod,
    placeholderData: (prev) => prev,
  })

  // extColumns 纳入 query key：勾选/取消扩展字段时需重新请求（带 ext_columns 参数）
  const kline = useQuery({
    // signalsEnabled 必须进 key: 打开信号标记要重新拉一次带 signal_* 列的响应
    queryKey: [...QK.kline(symbol, dateRange.start, dateRange.end, extColumns, period, adjust), signalsEnabled],
    queryFn: () => api.klineDaily(symbol, days, dateRange, extColumns, CUSTOM_INDICATORS, klineChartFields(signalsEnabled), period, adjust),
    enabled: !!symbol && !minutePeriod,
    placeholderData: (prev) => prev,
  })
  // 两条查询合一, 下游只读这一个(分钟周期时 kline 未启用, 数据为空)
  const klineData = minutePeriod ? klineMinute : kline

  const rows = useMemo(() => toOHLC(klineData.data?.rows ?? []), [klineData.data?.rows])
  rowsRef.current = rows
  const stockInfo = klineData.data?.stock_info
  const limitMarkers = useMemo(() => buildLimitUpMarkers(klineData.data?.rows ?? []), [klineData.data?.rows])
  /**
   * 外部事件(监控触发 / 回测买卖点) → ECharts 的 ChartMarker。
   *
   * ECharts 的 markPoint 不像 klinecharts overlay 那样能按槽位堆叠, 同一天多个
   * 标记只能合并成一个: 方向取「有 above 就 above」(告警/卖出优先浮在上方),
   * 颜色取第一个, 标签用「/」连前两个。这是与 KLinePro 唯一的有意差异 ——
   * 同一天堆 3 个 markPoint 在 ECharts 里会互相压住, 比合并更难读。
   */
  const eventChartMarkers = useMemo<ChartMarker[]>(() => {
    if (!eventMarks || eventMarks.length === 0 || period !== 'day') return []
    return eventMarks.map((p) => {
      const above = p.marks.some((m) => m.side === 'above')
      return {
        date: p.date,
        kind: 'neutral' as const,
        label: p.marks.map((m) => m.label).slice(0, 2).join('/'),
        above,
        color: p.marks[0]?.color,
      }
    })
  }, [eventMarks, period])

  const allMarkers = useMemo(() => [
    ...(markers ?? []),
    ...(showLimitMarkers ? limitMarkers : []),
    ...eventChartMarkers,
  ], [eventChartMarkers, limitMarkers, markers, showLimitMarkers])

  // 切换个股时载入该股已保存的手绘线
  useEffect(() => {
    setDrawLines(symbol ? loadLines(symbol) : [])
    setPreview(null)
    dragRef.current = null
  }, [symbol])

  // 画线: 在 zrender 上手动拖拽。起点/终点都 snap 到最近交易日的 x 位置,
  // 保证端点 date 一定落在当前 x 轴类目里(polylines 会丢弃不在轴上的顶点)。
  useEffect(() => {
    const chart = chartInst
    if (!chart) return
    const zr = chart.getZr()
    // ★ chartInst 可能是**已 dispose 的**实例: echarts 5.6.0 的 dispose() 会把
    //   _zr 置 null(echarts.js:824), 此时 getZr() 返回 null, 挂监听直接抛
    //   "Cannot read properties of null (reading 'on')"。
    //   触发窗口: 图表重建(容器高度变化)或组件卸载后, 父层 setChartInst 尚未刷新。
    //   崩在 useEffect 里会被 React Router 默认错误边界整页替换成堆栈, 所以必须守。
    if (!zr) return

    const toData = (ev: any) => {
      const pt = chart.convertFromPixel({ gridIndex: 0 }, [ev.offsetX, ev.offsetY])
      const d = rowsRef.current
      if (!pt || !d.length) return null
      const idx = Math.max(0, Math.min(d.length - 1, Math.round(Number(pt[0]))))
      if (!Number.isFinite(Number(pt[1]))) return null
      return {
        x: ev.offsetX, y: ev.offsetY,
        date: d[idx].date,
        price: Math.round(Number(pt[1]) * 100) / 100,
      }
    }

    const onDown = (ev: any) => {
      if (!drawingRef.current) return
      const p = toData(ev)
      if (!p) return
      dragRef.current = p
      setPreview({ x1: p.x, y1: p.y, x2: p.x, y2: p.y })
    }
    const onMove = (ev: any) => {
      if (!drawingRef.current || !dragRef.current) return
      setPreview(prev => (prev ? { ...prev, x2: ev.offsetX, y2: ev.offsetY } : prev))
    }
    const onUp = (ev: any) => {
      const start = dragRef.current
      dragRef.current = null
      setPreview(null)
      if (!drawingRef.current || !start) return
      const p = toData(ev)
      if (!p) return
      // 误点(同一位置)不成线
      if (p.date === start.date && Math.abs(p.price - start.price) < 1e-9) return
      setDrawLines(prev => {
        const next = [...prev, { a: { date: start.date, price: start.price }, b: { date: p.date, price: p.price } }]
        saveLines(symbol, next)
        return next
      })
    }
    // 鼠标在画布外松开时 zrender 收不到 mouseup, 兜底挂到 window
    const onWindowUp = () => { dragRef.current = null; setPreview(null) }

    zr.on('mousedown', onDown)
    zr.on('mousemove', onMove)
    zr.on('mouseup', onUp)
    window.addEventListener('mouseup', onWindowUp)
    return () => {
      zr.off('mousedown', onDown)
      zr.off('mousemove', onMove)
      zr.off('mouseup', onUp)
      window.removeEventListener('mouseup', onWindowUp)
    }
  }, [chartInst, symbol])

  /**
   * 受控的「主图定量结构」合并进指标清单。
   * 用派生而不是 useEffect 回写, 避免"点了按钮又被同步回来"的回弹。
   */
  const effectiveIndicators = useMemo(() => {
    if (structureOverlay === undefined) return activeIndicators
    const has = activeIndicators.includes('structure')
    if (structureOverlay === has) return activeIndicators
    return structureOverlay
      ? [...activeIndicators, 'structure']
      : activeIndicators.filter(k => k !== 'structure')
  }, [activeIndicators, structureOverlay])

  const toggleIndicator = useCallback((key: string) => {
    setActiveIndicators(prev => prev.includes(key) ? prev.filter(k => k !== key) : [...prev, key])
  }, [])

  const toggleStructure = useCallback(() => {
    const next = !effectiveIndicators.includes('structure')
    if (onStructureChange) {
      onStructureChange(next)
      return
    }
    setActiveIndicators(prev => next ? [...prev, 'structure'] : prev.filter(k => k !== 'structure'))
  }, [effectiveIndicators, onStructureChange])

  const updateVolumeCompare = useCallback((patch: Partial<VolumeCompareConfig>) => {
    setVolumeCompare(prev => {
      const next = normalizeVolumeCompare({ ...prev, ...patch })
      storage.stockVolumeCompare.set(next)
      return next
    })
  }, [])

  // 手绘线 -> 主图折线。非日线周期不显示: 端点 date 是日线日期, 周/月轴上找不到。
  const userPolylines = useMemo<ChartPolyline[]>(() => {
    if (period !== 'day') return []
    return drawLines.map(l => {
      const pts = [l.a, l.b].slice().sort((m, n) => (m.date < n.date ? -1 : m.date > n.date ? 1 : 0))
      return { points: pts.map(p => ({ date: p.date, price: p.price })), color: DRAW_COLOR, width: 1.5, name: 'user' }
    })
  }, [drawLines, period])

  const allPolylines = useMemo<ChartPolyline[]>(
    () => [...(period === 'day' ? polylines ?? [] : []), ...userPolylines],
    [period, polylines, userPolylines],
  )

  const clearLines = useCallback(() => {
    setDrawLines([])
    saveLines(symbol, [])
  }, [symbol])

  const activeSubDefs = effectiveIndicators
    .map(key => SUB_CHARTS.find(s => s.key === key))
    .filter((d): d is typeof SUB_CHARTS[number] => !!d)
  let subExtraH = 0
  activeSubDefs.forEach(def => { subExtraH += SUB_INFO_H + def.height })
  if (activeSubDefs.length > 0) subExtraH += activeSubDefs.length * SUB_GAP + 14
  const chartHeight = height + subExtraH

  useEffect(() => {
    onDataChange?.({ rows, rawRows: klineData.data?.rows ?? [], stockInfo, name: klineData.data?.name })
  }, [klineData.data?.name, klineData.data?.rows, onDataChange, rows, stockInfo])

  if (!symbol) return null

  return (
    <div className={className} style={{ minHeight: chartHeight }}>
      {showIndicatorControls && rows.length > 0 && (
        <div className="flex items-center gap-1.5 px-1 pb-0.5">
          {/* 周期: 日 / 周 / 月 */}
          <div className="flex items-center gap-0.5 rounded border border-border/70 p-0.5">
            {PERIOD_OPTIONS.map(opt => (
              <button
                key={opt.key}
                onClick={() => applyPeriod(opt.key)}
                title={periodTabTitle(opt)}
                className={`px-2 py-0.5 rounded text-[10px] font-mono cursor-pointer transition-colors ${
                  period === opt.key
                    ? 'bg-accent text-white'
                    : 'text-muted hover:text-secondary'
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>
          {/* 复权: 前复权 / 不复权 / 后复权 */}
          <div className="flex items-center gap-0.5 rounded border border-border/70 p-0.5">
            {ADJUST_OPTIONS.map(opt => (
              <button
                key={opt.key}
                onClick={() => applyAdjust(opt.key)}
                title={opt.title}
                className={`px-2 py-0.5 rounded text-[10px] font-mono cursor-pointer transition-colors ${
                  adjust === opt.key
                    ? 'bg-accent text-white'
                    : 'text-muted hover:text-secondary'
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>
          <div className="h-4 w-px bg-border/70" />
          {/* 画线: 开启后在 K 线区按住拖出趋势线, 端点自动吸到最近交易日 */}
          <button
            onClick={() => { setDrawing(v => !v); setPreview(null); dragRef.current = null }}
            title={drawing ? '退出画线模式' : '画线: 在 K 线区按住鼠标拖出趋势线'}
            className={`px-2 py-0.5 rounded text-[10px] font-mono cursor-pointer transition-colors ${
              drawing ? 'bg-accent text-white' : 'bg-elevated text-muted hover:text-secondary'
            }`}
          >
            {drawing ? '画线中' : '画线'}
          </button>
          {drawLines.length > 0 && (
            <button
              onClick={clearLines}
              title={`清除本股已画的 ${drawLines.length} 条线`}
              className="px-2 py-0.5 rounded text-[10px] font-mono cursor-pointer bg-elevated text-muted hover:text-danger transition-colors"
            >
              清除({drawLines.length})
            </button>
          )}

          {/* 右侧: 主图叠加开关 (蛟龙出海/主图定量结构画在 K 线上, 与缠论/异动同组),
              分钟周期下禁用(这两组信号只有日/周/月口径) */}
          <div className="ml-auto flex items-center gap-1.5">
            {!hideOverlayToggles && OVERLAY_INDICATORS.filter(ind => TOP_TOOLBAR_OVERLAYS.includes(ind.key)).map(ind => {
              const minuteDisabled = isMinutePeriod(period)
              const active = effectiveIndicators.includes(ind.key)
              return (
                <button
                  key={ind.key}
                  onClick={() => (ind.key === 'structure' ? toggleStructure() : toggleIndicator(ind.key))}
                  disabled={minuteDisabled}
                  title={minuteDisabled
                    ? `${ind.label}仅在日/周/月周期下可用`
                    : active ? `隐藏${ind.label}` : `显示${ind.label}`}
                  className={`px-2 py-0.5 rounded text-[10px] font-mono transition-colors ${
                    minuteDisabled
                      ? 'bg-elevated text-muted/40 cursor-not-allowed'
                      : active
                        ? 'text-accent bg-accent/15 cursor-pointer'
                        : 'bg-elevated text-muted hover:text-secondary cursor-pointer'
                  }`}
                >
                  {ind.label}
                </button>
              )
            })}
            {(showMarkerToggle && showLimitMarkers) || chanEnabled !== undefined ? (
              <>
              {showMarkerToggle && showLimitMarkers && (
                <button
                  onClick={() => setShowMarkers(v => !v)}
                  className={`px-2 py-0.5 rounded text-[10px] font-mono cursor-pointer transition-colors ${
                    showMarkers
                      ? 'text-[#FACC15] bg-[#FACC15]/10'
                      : 'bg-elevated text-muted hover:text-secondary'
                  }`}
                >
                  异动
                </button>
              )}
              {!hideOverlayToggles && chanEnabled !== undefined && onToggleChan !== undefined && (
                <button
                  onClick={onToggleChan}
                  disabled={period !== 'day'}
                  title={period !== 'day'
                    ? '缠论基于日线笔/中枢, 仅在日K周期下可用'
                    : chanEnabled ? '隐藏缠论笔/中枢/买卖点' : '显示缠论笔/中枢/买卖点'}
                  className={`px-2 py-0.5 rounded text-[10px] font-mono transition-colors ${
                    period !== 'day'
                      ? 'bg-elevated text-muted/40 cursor-not-allowed'
                      : chanEnabled
                        ? 'text-accent bg-accent/15 cursor-pointer'
                        : 'bg-elevated text-muted hover:text-secondary cursor-pointer'
                  }`}
                >
                  缠论
                </button>
              )}
              </>
            ) : null}
          </div>
        </div>
      )}
      {klineData.isLoading && <div className="text-sm text-muted py-4">加载中…</div>}
      {klineData.isError && <div className="text-sm text-danger py-2">日K加载失败</div>}
      {!klineData.isLoading && !klineData.isError && (klineData.data?.rows?.length ?? 0) > 0 && rows.length === 0 && (
        <div className="text-sm text-danger py-2">数据格式异常，请刷新页面</div>
      )}
      {rows.length > 0 && (
        <div className={`relative ${drawing ? 'cursor-crosshair' : ''}`}>
          <EChartsCandlestick
            data={rows}
            signalsEnabled={signalsEnabled}
            markers={allMarkers}
            ranges={ranges}
            priceLines={priceLines}
            // 缠论的笔顶点是「日线日期」, 在周/月 K 上大部分找不到对应 x 轴位置,
            // 强行插值会画出错误的斜线, 所以非日线周期下不叠加。
            polylines={allPolylines}
            height={chartHeight - 22}
            showMA={showMA}
            showInfoBar={showInfoBar}
            showMarkers={showMarkers}
            stockInfo={stockInfo}
            symbol={symbol}
            linkedPrice={linkedPrice}
            // 画线模式下不派发点击日期, 避免拖线时被日期点击抢走
            onDateClick={drawing ? undefined : onDateClick}
            onPriceDoubleClick={onPriceDoubleClick}
            onChartReady={setChartInst}
            visibleBars={visibleBars}
            activeIndicators={effectiveIndicators}
            volumeCompare={volumeCompare}
          />
          {/* 拖拽预览: 用 SVG 覆盖层画, 不进 ECharts 避免整图重绘 */}
          {preview && (
            <svg className="pointer-events-none absolute inset-0 h-full w-full">
              <line
                x1={preview.x1} y1={preview.y1} x2={preview.x2} y2={preview.y2}
                stroke={DRAW_COLOR} strokeWidth={1.5}
              />
            </svg>
          )}
        </div>
      )}
      {/* 底部指标快捷栏: 与顶部「数据口径」分开, 专管「图上显示什么」 */}
      {showIndicatorControls && rows.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 px-1 pt-1">
          {SUB_CHARTS.map(ind => (
            <button
              key={ind.key}
              onClick={() => toggleIndicator(ind.key)}
              className={`px-2 py-0.5 rounded text-[10px] font-mono cursor-pointer transition-colors ${
                effectiveIndicators.includes(ind.key)
                  ? 'bg-accent/20 text-accent'
                  : 'bg-elevated text-muted hover:text-secondary'
              }`}
            >
              {ind.label}
            </button>
          ))}
          <div className="h-4 w-px bg-border/70" />
          {/* 蛟龙出海/主图定量结构已上移到顶部工具条, 这里只剩 BOLL 等主图叠加 */}
          {OVERLAY_INDICATORS.filter(ind => !TOP_TOOLBAR_OVERLAYS.includes(ind.key)).map(ind => (
            <button
              key={ind.key}
              onClick={() => (ind.key === 'structure' ? toggleStructure() : toggleIndicator(ind.key))}
              className={`px-2 py-0.5 rounded text-[10px] font-mono cursor-pointer transition-colors ${
                effectiveIndicators.includes(ind.key)
                  ? 'bg-accent/20 text-accent'
                  : 'bg-elevated text-muted hover:text-secondary'
              }`}
            >
              {ind.label}
            </button>
          ))}
          {effectiveIndicators.includes('vol') && (
            <div className="ml-0.5 flex h-5 items-center gap-1.5 border-l border-border/70 pl-2">
              <span className="text-[10px] text-muted">量比</span>
              <button
                type="button"
                role="switch"
                aria-checked={volumeCompare.enabled}
                aria-label="开启量能对比"
                title={volumeCompare.enabled ? '关闭量能对比' : '开启量能对比'}
                onClick={() => updateVolumeCompare({ enabled: !volumeCompare.enabled })}
                className={`relative h-3.5 w-6 shrink-0 rounded-full transition-colors ${
                  volumeCompare.enabled ? 'bg-accent' : 'bg-elevated'
                }`}
              >
                <span className={`absolute left-0 top-0.5 h-2.5 w-2.5 rounded-full bg-white transition-transform ${
                  volumeCompare.enabled ? 'translate-x-3' : 'translate-x-0.5'
                }`} />
              </button>
              <select
                aria-label="量能对比周期"
                value={volumeCompare.days}
                disabled={!volumeCompare.enabled}
                onChange={event => updateVolumeCompare({ days: Number(event.target.value) })}
                className="h-5 rounded border border-border bg-base px-1 text-[10px] text-secondary outline-none disabled:opacity-40"
              >
                {Array.from({ length: 20 }, (_, index) => index + 1).map(days => (
                  <option key={days} value={days}>前{days}日均量</option>
                ))}
              </select>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
