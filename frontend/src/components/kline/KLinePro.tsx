/**
 * KLineChart 内核的个股 K 线组件。
 *
 * S1: 多周期打通 —— 日 / 周 / 月 + 1m / 5m / 15m / 30m / 60m / 90m / 120m。
 *   - 日/周/月 走 /api/kline/daily(周月由后端按日 K 聚合并重算指标)
 *   - 分钟档 走 /api/kline/minute-k, 后端两级数据源(响应 source 字段标明):
 *       preagg = 预聚合周期目录(可回溯到 2025-01), local = 1m 现场聚合
 *
 * ★ 时间戳口径(与 ECharts 的 formatMinuteTime 同判据, 别改成无条件 +8):
 *   后端分钟 datetime 有两种口径并存 ——
 *     naive UTC(北京 09:30 记成 01:30) 与 北京墙钟(09:30 记成 09:30)。
 *   判别: hour < 8 视为前者(它本身就是真实 epoch), hour >= 8 视为后者(需减 8h)。
 *   图表再 setTimezone('Asia/Shanghai'), 保证 x 轴/十字线按北京时间显示。
 *
 * ★ klinecharts v10 不做周期聚合(源码里没有 aggregate): setPeriod 只影响
 *   轴标签/十字线的时间格式。所以聚合必须由后端完成, 前端只负责喂数。
 *
 * S2: 主副图窗格 + 指标管理 —— 指标清单持久化在 localStorage(换股不换指标),
 *   主图指标挂 candle_pane, 副图自动开新窗格; 窗格分隔条可拖动, 高度同样持久化。
 *   指标参数**不硬编码**: 创建时不传 calcParams, 再从 getIndicators() 回读库内默认值。
 *
 * 当前能力: 多周期 K + 指标自选(27 个内置) + 缠论叠加(仅日线档) + 主图定量结构
 *   (EMA25/89 双轨 + 交叉图标 + 九转) + 筹码分布 + 监控价位水平线 + 三档复权 + 主题跟随。
 * 未做: 涨停标记、手绘线、分时(均价)图、MACD 定量结构副图。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import * as kc from 'klinecharts'
import { api, KLINE_CHART_FIELDS, klineChartFields, type KlineRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useChanOverlay } from '@/lib/useChanOverlay'
import { useChartTheme } from '@/lib/theme'
import { chartSession } from '@/lib/chartSession'
import { applyKcViewport, readKcViewport } from '@/lib/chartViewport'
import type { ChartEventPoint } from '@/lib/chart-events'
import { chartBars, chartFocus, chartSignals } from '@/lib/chartBridge'
import { findDateIndex, signalRowsToTimeline } from '@/lib/chart-timeline'
import type { ChartPriceLine } from '@/lib/chart-primitives'
import {
  ADJUST_OPTIONS,
  isMinutePeriod,
  MINUTE_SPAN,
  periodTabsFor,
  type KLineAdjust,
  type KLinePeriod,
} from '@/lib/klinePeriod'
import { registerChanOverlay } from './chan-overlay-kline'
import { registerPriceLineOverlay } from './price-line-overlay'
import { registerChipsOverlay } from './chips-overlay'
import { registerStructureOverlay, type StructurePayload } from './structure-overlay'
import {
  collectSignalIds,
  registerSignalMarkersOverlay,
  type SignalMarkersPayload,
} from './signal-markers'
import {
  registerEventMarkersOverlay,
  setEventMarks,
  type EventMarkersPayload,
} from './event-markers'
import { IndicatorManager } from './IndicatorManager'
import {
  MAIN_PANE_ID,
  loadIndicators,
  loadPaneHeights,
  saveIndicators,
  savePaneHeights,
  type IndicatorConfig,
} from '@/lib/klineIndicators'
import { cn } from '@/lib/cn'

const BULL = '#F04438' // --bull 红涨
const BEAR = '#12B76A' // --bear 绿跌
/** 十字光标线的颜色(双主题都用中性灰, 与 ECharts 的 CT().crosshair 观感对齐) */
const CROSSHAIR = '#475569'

const CUSTOM_INDICATORS = 'trend_dragon,capital_momentum,structure,macd_structure'

/** 分钟档回看天数(交给后端 days 参数) */
const MINUTE_LOOKBACK_DAYS = 120
/**
 * 分钟档只取最近 N 根。5m/120 交易日全量是 8640 根 ≈ 1MB, 图也塞不下;
 * 截断到 800 根 ≈ 80KB。指标是后端在**完整数据**上算好之后再截的, 不失真。
 */
const MINUTE_BAR_LIMIT = 800

/** 筹码分布回望交易日数 / 价格档数 */
const CHIPS_DAYS = 250
const CHIPS_BINS = 60

/** 图内周期工具条: 与 ECharts 内核消费同一份档位表(见 lib/klinePeriod) */
const PERIOD_TABS = periodTabsFor('klinecharts')

const CN_OFFSET_MS = 8 * 60 * 60 * 1000

/**
 * 主题 → klinecharts styles。
 * 此前这里硬编码了一套暗色(#0B1220), 切到亮色主题后 K 线区仍是黑底 ——
 * 这是「切换内核割裂」里最扎眼的一条。现在跟 ECharts 一样走 useChartTheme()。
 */
function buildStyles(ct: { grid: string; text: string; crosshairLabelBg: string }): kc.DeepPartial<kc.Styles> {
  const grid = ct.grid
  const text = ct.text
  return {
    grid: { horizontal: { color: grid }, vertical: { color: grid } },
    candle: {
      bar: { upColor: BULL, downColor: BEAR, noChangeColor: text },
      priceMark: { last: { upColor: BULL, downColor: BEAR, noChangeColor: text } },
    },
    xAxis: { axisLine: { color: grid }, tickLine: { color: grid }, tickText: { color: text } },
    yAxis: { axisLine: { color: grid }, tickLine: { color: grid }, tickText: { color: text } },
    separator: { color: grid },
    crosshair: {
      horizontal: { text: { color: text, backgroundColor: ct.crosshairLabelBg }, line: { color: CROSSHAIR } },
      vertical: { text: { color: text, backgroundColor: ct.crosshairLabelBg }, line: { color: CROSSHAIR } },
    },
  }
}

/**
 * 后端日期 -> 毫秒 epoch。
 * 兼容三种形态: 纯日期(日/周/月)、带时间的分钟戳、以及已是数字的时间戳。
 */
function parseTs(v: unknown): number | null {
  if (v == null) return null
  if (typeof v === 'number') return Number.isFinite(v) ? v : null
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?/.exec(String(v).trim())
  if (!m) return null
  const [, y, mo, d, hh, mi, ss] = m
  const ts = Date.UTC(+y, +mo - 1, +d, hh ? +hh : 0, mi ? +mi : 0, ss ? +ss : 0)
  if (!Number.isFinite(ts)) return null
  // hour < 8 => 源里存的是 naive UTC(北京 09:30 记成 01:30), 它本身就是真实 epoch
  // hour >= 8 => 源里存的是北京墙钟, 必须减 8h 才是真实 epoch
  return (hh ? +hh : 0) < 8 ? ts : ts - CN_OFFSET_MS
}

/**
 * 结构列(st_*): 主图定量结构的轨道与标注, 由后端算好后随 K 线一起下来。
 * 挂在 KLineData 上是为了让 structure overlay 能直接按 timestamp 取到 ——
 * 前端不重算指标, 与 ECharts 侧同源同口径。
 */
function rowToKLine(r: KlineRow): kc.KLineData | null {
  if (!r || r.date == null || r.open == null || r.close == null) return null
  const ts = parseTs(r.date)
  if (ts == null) return null
  return {
    timestamp: ts,
    open: Number(r.open),
    high: Number(r.high ?? r.close),
    low: Number(r.low ?? r.close),
    close: Number(r.close),
    volume: Number(r.volume ?? 0),
    sig: collectSignalIds(r),
    st_dsg: r.st_dsg ?? null,
    st_dxg: r.st_dxg ?? null,
    st_csg: r.st_csg ?? null,
    st_cxg: r.st_cxg ?? null,
    st_icon: r.st_icon ?? 0,
    st_dn: r.st_dn ?? 0,
    st_up: r.st_up ?? 0,
  }
}

function parseRows(rows: KlineRow[]): kc.KLineData[] {
  return rows.map(rowToKLine).filter((d): d is kc.KLineData => d !== null)
}

/** 从已创建的指标回读库内默认参数(避免硬编码, 见 klineIndicators 顶部说明) */
function readRealParams(ind: kc.Indicator | undefined): number[] | null {
  const cp = ind?.calcParams
  if (!Array.isArray(cp) || cp.length === 0) return null
  const nums = cp.filter((v): v is number => typeof v === 'number' && Number.isFinite(v))
  return nums.length === cp.length ? nums : null
}

/** 恢复上次拖动过的副图窗格高度(按副图顺序, paneId 是库内自增的, 不能当 key) */
function applyPaneHeights(chart: kc.Chart): void {
  const saved = loadPaneHeights()
  if (saved.length === 0) return
  const opts = chart.getPaneOptions()
  if (!Array.isArray(opts)) return
  opts.slice(1).forEach((p, i) => {
    const h = saved[i]
    if (h) chart.setPaneOptions({ id: p.id, height: h, dragEnabled: true })
  })
}

function toChartPeriod(p: KLinePeriod): kc.Period {
  const span = MINUTE_SPAN[p]
  if (span) return { type: 'minute', span }
  if (p === 'week') return { type: 'week', span: 1 }
  if (p === 'month') return { type: 'month', span: 1 }
  return { type: 'day', span: 1 }
}

export interface KLineProProps {
  symbol: string
  className?: string
  dateRange: { start: string; end: string }
  /** 当前周期; 不传则组件自己维护(内部工具条) */
  period?: KLinePeriod
  /** 受控模式: 由外层持有周期(终端键盘 1/2/3 与图内按钮共用一份状态) */
  onPeriodChange?: (p: KLinePeriod) => void
  /**
   * 复权方式(受控)。不传则组件自己维护。
   * 提升到终端层是为了让「切内核」不丢口径 —— 此前这里硬编码 qfq。
   */
  adjust?: KLineAdjust
  onAdjustChange?: (a: KLineAdjust) => void
  /**
   * 主图定量结构开关(受控)。不传则组件自己维护, 默认开 ——
   * 这是本项目的核心策略信号, 默认可见才符合「策略与结构的观察面」定位。
   */
  structureEnabled?: boolean
  onStructureChange?: (v: boolean) => void
  chanEnabled?: boolean
  priceLines?: ChartPriceLine[]
  /** 筹码分布开关(受控)。不传则组件内部维护, 默认关 */
  chipsEnabled?: boolean
  onChipsChange?: (v: boolean) => void
  /**
   * 策略信号标记开关(受控)。不传则组件内部维护, 默认关 ——
   * 打开要额外下发 20 多个 signal_* 布尔列(1000 根约 +300KB 未压缩),
   * 默认不付这个成本。打开后图上按买(红下三角)/卖(绿上三角)标出信号日。
   */
  signalsEnabled?: boolean
  onSignalsChange?: (v: boolean) => void
  /**
   * 外部事件标记(监控触发 / 回测买卖点), 已在终端层按日期归约好。
   * 数据不在 K 线行里, 由 overlay 从内部 WeakMap 按日期反查 —— 传空数组即关闭。
   * 两个内核消费同一份(见 lib/chart-events.ts), 避免切内核观感漂移。
   */
  eventMarks?: ChartEventPoint[]
  /**
   * true = 隐藏图内的叠加层开关(定量结构 / 筹码)。
   * 终端层已提供统一入口时传 true —— 同屏两组同名按钮本身就是割裂观感。
   */
  hideOverlayToggles?: boolean
  /** 盘中自动刷新间隔(毫秒); 分钟档配合实时同步使用 */
  refetchIntervalMs?: number
}

export function KLinePro({
  symbol,
  className,
  dateRange,
  period: periodProp,
  onPeriodChange,
  adjust: adjustProp,
  onAdjustChange,
  structureEnabled: structProp,
  onStructureChange,
  chanEnabled = false,
  priceLines = [],
  chipsEnabled: chipsProp,
  onChipsChange,
  signalsEnabled: signalsProp,
  onSignalsChange,
  eventMarks,
  hideOverlayToggles = false,
  refetchIntervalMs,
}: KLineProProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<kc.Chart | null>(null)
  const rowsRef = useRef<kc.KLineData[]>([])
  const [ready, setReady] = useState(false)
  const ct = useChartTheme()

  // ── S2 指标清单(持久化在 localStorage, 换股不换指标) ──
  const [indicators, setIndicators] = useState<IndicatorConfig[]>(() => loadIndicators())
  const [managerOpen, setManagerOpen] = useState(false)
  /** key -> 图表内指标 id / 所在窗格 */
  const indRefs = useRef(new Map<string, { id: string; paneId: string }>())
  /**
   * 各叠加层在图表内的 id。
   * ★ 换股会 dispose 重建图表, 这些 id 随之失效 —— 重建时必须清空, 否则后续
   *   走 overrideOverlay 分支会打到不存在的 id 上, 表现是「换股后叠加层静默消失」。
   */
  const overlayIds = useRef<
    Record<'chan' | 'chips' | 'price' | 'structure' | 'signal' | 'events', string | null>
  >({
    chan: null, chips: null, price: null, structure: null, signal: null, events: null,
  })
  /** 会话视口只重放一次(挂载/换股后), 之后交给用户自由滚动 */
  const vpAppliedRef = useRef(false)

  const [innerPeriod, setInnerPeriod] = useState<KLinePeriod>('day')
  const period = periodProp ?? innerPeriod
  const applyPeriod = useCallback((p: KLinePeriod) => {
    if (onPeriodChange) onPeriodChange(p)
    else setInnerPeriod(p)
  }, [onPeriodChange])
  const minutePeriod = isMinutePeriod(period)

  // 复权: 受控优先。分钟档后端无复权口径, 控件禁用(见工具条)
  const [innerAdjust, setInnerAdjust] = useState<KLineAdjust>('qfq')
  const adjust = adjustProp ?? innerAdjust
  const applyAdjust = useCallback((a: KLineAdjust) => {
    if (onAdjustChange) onAdjustChange(a)
    else setInnerAdjust(a)
  }, [onAdjustChange])

  // 主图定量结构: 受控优先, 默认开
  const [innerStruct, setInnerStruct] = useState(true)
  const structOn = structProp ?? innerStruct
  const applyStruct = useCallback((v: boolean) => {
    if (onStructureChange) onStructureChange(v)
    else setInnerStruct(v)
  }, [onStructureChange])

  // 策略信号标记: 受控优先, 默认关(打开要多下发 signal_* 列, 见 props 注释)
  const [innerSignals, setInnerSignals] = useState(false)
  const signalsOn = signalsProp ?? innerSignals
  const applySignals = useCallback((v: boolean) => {
    if (onSignalsChange) onSignalsChange(v)
    else setInnerSignals(v)
  }, [onSignalsChange])

  const days = useMemo(() => {
    const s = new Date(dateRange.start), e = new Date(dateRange.end)
    return Math.max(1, Math.ceil((e.getTime() - s.getTime()) / 86400000) + 1)
  }, [dateRange])

  const daily = useQuery({
    // signalsOn 必须进 key: 打开信号标记要重新拉一次带 signal_* 列的响应,
    // 否则命中旧缓存(没有信号列)会导致图上什么都不标。
    queryKey: [...QK.kline(symbol, dateRange.start, dateRange.end, undefined, period, adjust), signalsOn],
    queryFn: () => api.klineDaily(symbol, days, dateRange, undefined, CUSTOM_INDICATORS, klineChartFields(signalsOn), period, adjust),
    enabled: !!symbol && !minutePeriod,
  })

  const minuteK = useQuery({
    queryKey: QK.klineMinuteK(symbol, period, MINUTE_LOOKBACK_DAYS, MINUTE_BAR_LIMIT),
    queryFn: () => api.klineMinuteK(symbol, period, MINUTE_LOOKBACK_DAYS, KLINE_CHART_FIELDS, MINUTE_BAR_LIMIT),
    enabled: !!symbol && minutePeriod,
    refetchInterval: minutePeriod ? refetchIntervalMs : undefined,
  })

  // 两条查询互斥启用, 下游只读这一个
  const active = minutePeriod ? minuteK : daily

  const rows = useMemo(() => parseRows(active.data?.rows ?? []), [active.data?.rows])
  const chartDates = useMemo(
    () => (minutePeriod ? [] : rows.map(d => new Date(d.timestamp).toISOString().slice(0, 10))),
    [rows, minutePeriod],
  )

  // 缠论只在日线档有意义(笔/中枢按日线口径算), 分钟档直接关掉, 免得发无谓请求
  const chanLayers = useChanOverlay(symbol, chartDates, chanEnabled && !minutePeriod)

  // ── S3 筹码分布: 工具条开关控制, 只在日线档取(分钟档没有"持仓成本"意义) ──
  // 受控优先: 终端层持有后与结构/缠论一样进会话, 切内核不丢
  const [innerChips, setInnerChips] = useState(false)
  const chipsOn = chipsProp ?? innerChips
  const applyChips = useCallback((v: boolean) => {
    if (onChipsChange) onChipsChange(v)
    else setInnerChips(v)
  }, [onChipsChange])
  const chips = useQuery({
    queryKey: QK.stockChips(symbol, CHIPS_DAYS, CHIPS_BINS),
    queryFn: () => api.stockAnalysisChips(symbol, { days: CHIPS_DAYS, bins: CHIPS_BINS }),
    enabled: !!symbol && chipsOn && !minutePeriod,
    staleTime: 10 * 60 * 1000,
  })
  const chipsData = useMemo(() => {
    const d = chips.data
    if (!d?.ok || !d.bins?.length) return null
    return { bins: d.bins, close: d.close, avg_cost: d.avg_cost, step: d.step }
  }, [chips.data])

  // 初始化图表（仅一次）
  useEffect(() => {
    const el = containerRef.current
    if (!el || chartRef.current) return
    // 换股会重建图表: 先把 ready 打回 false, 否则 setReady(true) 同值不触发重渲染,
    // 指标 diff 的 effect 不会重跑, 新图上就一个指标都没有。
    setReady(false)
    indRefs.current.clear()
    overlayIds.current = {
      chan: null, chips: null, price: null, structure: null, signal: null, events: null,
    }
    vpAppliedRef.current = false
    registerChanOverlay()
    registerPriceLineOverlay()
    registerChipsOverlay()
    registerStructureOverlay()
    registerSignalMarkersOverlay()
    registerEventMarkersOverlay()

    const chart = kc.init(el, { styles: buildStyles(ct) })
    if (!chart) return
    chartRef.current = chart

    // 后端给的是北京时间口径的墙钟/naive-UTC 字符串, 已统一转成真实 epoch;
    // 这里指定时区, 让 x 轴与十字线按北京时间渲染。
    chart.setTimezone('Asia/Shanghai')

    chart.setDataLoader({
      getBars: ({ type, callback }) => {
        if (type === 'init') callback(rowsRef.current, { backward: false, forward: false })
        else callback([], false)
      },
    })
    chart.setSymbol({ ticker: symbol, pricePrecision: 2, volumePrecision: 0 })
    chart.setPeriod(toChartPeriod(period))
    setReady(true)

    return () => {
      kc.dispose(el)
      chartRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol])

  // 主题切换: 只换 styles, 不重建图表(重建会丢视口 / 指标 / 窗格高度)
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !ready) return
    chart.setStyles(buildStyles(ct))
  }, [ct, ready])

  // 数据刷新
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    rowsRef.current = rows
    chart.resetData()
  }, [rows])

  // ── 视口: 静默写回会话 ──
  // 滚动/缩放每秒可触发几十次, 绝不能走 setState —— 写 chartSession 的静默通道,
  // 只在新渲染器挂载时被读走一次(见下)。这是「切内核不丢缩放」的关键。
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !ready) return
    const report = () => {
      const vp = readKcViewport(chart)
      if (vp) chartSession.setViewport(vp)
    }
    chart.subscribeAction('onVisibleRangeChange', report)
    chart.subscribeAction('onScroll', report)
    chart.subscribeAction('onZoom', report)
    return () => {
      chart.unsubscribeAction('onVisibleRangeChange', report)
      chart.unsubscribeAction('onScroll', report)
      chart.unsubscribeAction('onZoom', report)
    }
  }, [ready])

  // 挂载/换股后重放上次视口(仅一次, 且要等数据到位 —— 空图上算不出 barSpace)
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !ready || rows.length === 0 || vpAppliedRef.current) return
    const vp = chartSession.getViewport()
    if (vp) applyKcViewport(chart, vp)
    vpAppliedRef.current = true
  }, [ready, rows.length])

  // 周期切换: setPeriod 内部会 resetData 重新走 loader, 先清空 rowsRef
  // 避免切档瞬间用上一档的数据重绘(会看到错周期的 K 线)。
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !ready) return
    rowsRef.current = []
    chart.setPeriod(toChartPeriod(period))
  }, [period, ready])

  // 缠论叠加层：创建一次，之后用 overrideOverlay 更新 extendData
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !ready) return
    const payload = {
      polylines: chanLayers.polylines,
      ranges: chanLayers.ranges,
      markers: chanLayers.markers,
    }
    if (!overlayIds.current.chan) {
      const first = rowsRef.current[0]
      if (!first) return
      const id = chart.createOverlay({
        name: 'chan',
        paneId: 'candle_pane',
        points: [{ timestamp: first.timestamp, value: first.close }],
        extendData: payload,
      })
      if (typeof id === 'string') overlayIds.current.chan = id
    } else {
      chart.overrideOverlay({ id: overlayIds.current.chan, extendData: payload })
    }
  }, [chanLayers, ready])

  // 筹码分布: 创建一次, override 更新 extendData
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !ready) return
    if (!chipsData) {
      // 关掉开关时要真的移除, 否则图上残留旧筹码
      if (overlayIds.current.chips) {
        chart.removeOverlay({ id: overlayIds.current.chips })
        overlayIds.current.chips = null
      }
      return
    }
    if (!overlayIds.current.chips) {
      const first = rowsRef.current[0]
      if (!first) return
      const id = chart.createOverlay({
        name: 'chips',
        paneId: 'candle_pane',
        points: [{ timestamp: first.timestamp, value: first.close }],
        extendData: chipsData,
      })
      if (typeof id === 'string') overlayIds.current.chips = id
    } else {
      chart.overrideOverlay({ id: overlayIds.current.chips, extendData: chipsData })
    }
  }, [chipsData, ready])

  // 监控价位线：创建一次，override 更新
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !ready) return
    if (!overlayIds.current.price) {
      const first = rowsRef.current[0]
      if (!first) return
      const id = chart.createOverlay({
        name: 'priceLine',
        paneId: 'candle_pane',
        points: [{ timestamp: first.timestamp, value: first.close }],
        extendData: priceLines,
      })
      if (typeof id === 'string') overlayIds.current.price = id
    } else {
      chart.overrideOverlay({ id: overlayIds.current.price, extendData: priceLines })
    }
  }, [priceLines, ready])

  // ── 主图定量结构: 与 ECharts 侧 showStructure 同一套口径 ──
  //   (EMA25/89 双轨 + 轨道带 + BBB/SSS 交叉图标 + 九转数字)
  //   数据从 getDataList() 读, 这里只用 rev 触发重绘。
  const structRev = useMemo(() => {
    const last = rows[rows.length - 1]
    return `${symbol}|${period}|${rows.length}|${last?.timestamp ?? 0}`
  }, [symbol, period, rows])
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !ready) return
    // 结构信号按日线口径算, 分钟档不画(与 ECharts 侧一致, 按钮也是禁用的)
    if (!structOn || minutePeriod) {
      if (overlayIds.current.structure) {
        chart.removeOverlay({ id: overlayIds.current.structure })
        overlayIds.current.structure = null
      }
      return
    }
    const payload: StructurePayload = { rev: structRev }
    if (!overlayIds.current.structure) {
      const first = rowsRef.current[0]
      if (!first) return
      const id = chart.createOverlay({
        name: 'structure',
        paneId: 'candle_pane',
        points: [{ timestamp: first.timestamp, value: first.close }],
        extendData: payload,
      })
      if (typeof id === 'string') overlayIds.current.structure = id
    } else {
      chart.overrideOverlay({ id: overlayIds.current.structure, extendData: payload })
    }
  }, [structOn, structRev, minutePeriod, ready])

  // ── 策略信号标记: 与 ECharts 侧同一套口径(买=红下三角 / 卖=绿上三角) ──
  // 数据挂在 KLineData.sig 上, overlay 从 getDataList() 读, 这里只用 rev 触发重绘
  // (复用 structRev —— 它已经是「标的+周期+行数+末根」的数据版本指纹)。
  // 只有日线档有信号: 周/月线是聚合结果(只保留 OHLCV), 分钟档压根没有这些列。
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !ready) return
    if (!signalsOn || period !== 'day') {
      if (overlayIds.current.signal) {
        chart.removeOverlay({ id: overlayIds.current.signal })
        overlayIds.current.signal = null
      }
      return
    }
    const payload: SignalMarkersPayload = { rev: structRev }
    if (!overlayIds.current.signal) {
      const first = rowsRef.current[0]
      if (!first) return
      const id = chart.createOverlay({
        name: 'signalMarkers',
        paneId: 'candle_pane',
        points: [{ timestamp: first.timestamp, value: first.close }],
        extendData: payload,
      })
      if (typeof id === 'string') overlayIds.current.signal = id
    } else {
      chart.overrideOverlay({ id: overlayIds.current.signal, extendData: payload })
    }
  }, [signalsOn, period, structRev, ready])

  // ── 外部事件标记(监控触发 / 回测买卖点) ──
  // 数据是终端层异步取来的, 不在 K 线行里, 所以走 WeakMap 挂到 chart 实例:
  // 开关时只改 WeakMap + 推一个 rev, 不重建 dataList(那是用户最常点的动作)。
  // 同样只在日线档画: 告警/买卖点都是日粒度, 周月线的 date 是周末, 对不上。
  //
  // rev 必须含首末日期: 只写点数的话, 「告警条数不变但内容换了」(老告警过期、
  // 新告警补位) 不会触发重绘, 图上就一直标着旧的。
  const eventRev = useMemo(
    () => (eventMarks && eventMarks.length > 0
      ? `${eventMarks.length}:${eventMarks[0].date}:${eventMarks[eventMarks.length - 1].date}`
      : ''),
    [eventMarks],
  )
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !ready) return
    const marks = period === 'day' ? eventMarks : undefined
    if (!marks || marks.length === 0) {
      if (overlayIds.current.events) {
        chart.removeOverlay({ id: overlayIds.current.events })
        overlayIds.current.events = null
      }
      setEventMarks(chart, null)
      return
    }
    const first = rowsRef.current[0]
    if (!first) return
    setEventMarks(chart, marks)
    const payload: EventMarkersPayload = { rev: eventRev }
    if (!overlayIds.current.events) {
      const id = chart.createOverlay({
        name: 'eventMarkers',
        paneId: 'candle_pane',
        points: [{ timestamp: first.timestamp, value: first.close }],
        extendData: payload,
      })
      if (typeof id === 'string') overlayIds.current.events = id
    } else {
      chart.overrideOverlay({ id: overlayIds.current.events, extendData: payload })
    }
  }, [eventMarks, eventRev, period, ready])

  // ── 事件时间轴(P4-1): 上报日期序列与信号事件 ──────────────────
  // 时间轴挂在终端层, 拿不到渲染器内部的 rows; 反过来让终端层再拉一次日K是重复
  // 请求。所以只上报「一条日期数组 + 已归约好的信号事件」, 两边指纹相同就不通知
  // (分钟档 6 秒一次的轮询不会因此把终端重渲染一遍)。
  const signalTimeline = useMemo(
    () => (signalsOn && period === 'day' ? signalRowsToTimeline(active.data?.rows ?? []) : []),
    [signalsOn, period, active.data?.rows],
  )
  useEffect(() => { chartBars.set(chartDates) }, [chartDates])
  useEffect(() => { chartSignals.set(signalTimeline) }, [signalTimeline])

  // ── 点击定位(P4-2): 时间轴点某天 → 视口挪过去 ────────────────
  //
  // ★ scrollToDataIndex 的语义是实测出来的, 别按直觉写: 它是把参数 j 放在视口
  //   **右端**(实测 to ≈ j + 2, 那 2 根是右侧留白), 不是左端。所以想让目标落在
  //   anchor 处(0=最左, 1=最右), 得先把它往右推 (1 - anchor) 屏再减掉留白。
  //   照直觉写成 idx - anchor*屏宽 的话, 目标会被推到视口左侧外面去。
  //
  // 依赖里带 chartDates 是因为「数据还没到就不能消费指令」—— 那时 return 而不
  // consume, 等数据到了本 effect 重跑, 订阅时会自动补发那条待办指令。
  useEffect(() => {
    if (!ready) return
    return chartFocus.subscribe((req) => {
      const chart = chartRef.current
      const dates = chartBars.get()
      if (!chart || dates.length === 0) return
      const idx = findDateIndex(dates, req.date)
      if (idx < 0) return
      const range = chart.getVisibleRange()
      const vis = Math.max(1, range.to - range.from + 1)
      const j = idx + Math.round((vis - 1) * (1 - req.anchor)) - 2
      chart.scrollToDataIndex(Math.max(0, j), 300)
      chartFocus.consume()
    })
  }, [ready, chartDates])

  // ── S2: 指标清单 diff 到图表 ──
  // 增删改一律走增量, 不做「全量重建」 —— 重建窗格会把用户拖动过的高度一起丢掉。
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !ready) return
    const refs = indRefs.current
    const alive = new Set(indicators.map(c => c.key))
    const defaults: IndicatorConfig[] = []

    for (const [key, ref] of Array.from(refs.entries())) {
      if (alive.has(key)) continue
      chart.removeIndicator({ id: ref.id })
      refs.delete(key)
    }

    for (const c of indicators) {
      const ref = refs.get(c.key)
      if (!ref) {
        const payload: kc.IndicatorCreate = { name: c.name }
        // 主图指标显式挂到 K 线窗格; 副图不传 paneId, 库会自动新开一个窗格
        if (c.group === 'main') payload.paneId = MAIN_PANE_ID
        if (c.params.length > 0) payload.calcParams = c.params
        const id = chart.createIndicator(payload)
        if (!id) continue
        const ind = chart.getIndicators({ id })[0]
        refs.set(c.key, { id, paneId: ind?.paneId ?? '' })
        if (c.params.length === 0) {
          const real = readRealParams(ind)
          if (real) defaults.push({ ...c, params: real })
        }
      } else if (c.params.length > 0) {
        chart.overrideIndicator({ id: ref.id, name: c.name, calcParams: c.params })
      }
    }

    // 首次创建时把库内默认参数回写进状态(这样管理面板才显示得出输入框)
    if (defaults.length > 0) {
      setIndicators(prev => prev.map(c => defaults.find(d => d.key === c.key) ?? c))
    }
    applyPaneHeights(chart)
  }, [indicators, ready])

  // 指标清单落盘。默认清单也要写 —— 否则 store 只有用户改过之后才有值,
  // 排查时看到的是空 localStorage, 与图上实际有指标对不上。
  useEffect(() => { saveIndicators(indicators) }, [indicators])

  // 拖动副图分隔条 -> 持久化高度(防抖 200ms)
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !ready) return
    let timer: number | undefined
    const onDrag = () => {
      window.clearTimeout(timer)
      timer = window.setTimeout(() => {
        const opts = chart.getPaneOptions()
        if (!Array.isArray(opts)) return
        savePaneHeights(opts.slice(1).map(p => p.height))
      }, 200)
    }
    chart.subscribeAction('onPaneDrag', onDrag)
    return () => {
      window.clearTimeout(timer)
      chart.unsubscribeAction('onPaneDrag', onDrag)
    }
  }, [ready])

  return (
    <div className={cn('relative flex h-full w-full flex-col', className)}>
      <div className="flex shrink-0 flex-wrap items-center gap-1 px-1 py-1">
        {PERIOD_TABS.map(t => (
          <button
            key={t.key}
            type="button"
            onClick={() => applyPeriod(t.key)}
            className={cn(
              'h-6 rounded border px-1.5 text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent',
              t.key === period
                ? 'border-accent/30 bg-accent/20 font-medium text-accent'
                : 'border-transparent text-muted hover:bg-elevated hover:text-foreground',
            )}
          >
            {t.label}
          </button>
        ))}
        {/* 复权: 与 ECharts 侧同一份 ADJUST_OPTIONS。分钟档后端无复权口径, 禁用 */}
        <div className="ml-1 flex items-center gap-0.5 rounded border border-border/70 p-0.5">
          {ADJUST_OPTIONS.map(opt => (
            <button
              key={opt.key}
              type="button"
              onClick={() => applyAdjust(opt.key)}
              disabled={minutePeriod}
              title={minutePeriod ? '分钟档无复权口径' : opt.title}
              className={cn(
                'h-5 rounded px-1.5 text-[10px] font-mono transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent',
                minutePeriod
                  ? 'cursor-not-allowed text-muted/40'
                  : adjust === opt.key
                    ? 'bg-accent text-white'
                    : 'text-muted hover:text-secondary',
              )}
            >
              {opt.label}
            </button>
          ))}
        </div>
        {!hideOverlayToggles && (
          <button
            type="button"
            onClick={() => applyStruct(!structOn)}
            disabled={minutePeriod}
            title={minutePeriod
              ? '主图定量结构(双轨/九转)按日线口径算, 分钟档不可用'
              : structOn ? '隐藏主图定量结构(EMA25/89 双轨 + 九转)' : '显示主图定量结构(EMA25/89 双轨 + 九转)'}
            className={cn(
              'h-6 rounded border px-1.5 text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent',
              minutePeriod
                ? 'cursor-not-allowed border-transparent text-muted/40'
                : structOn
                  ? 'border-accent/30 bg-accent/20 font-medium text-accent'
                  : 'border-transparent text-muted hover:bg-elevated hover:text-foreground',
            )}
          >
            定量结构
          </button>
        )}
        <button
          type="button"
          onClick={() => setManagerOpen(v => !v)}
          title="指标设置(主图 / 副图、参数、窗格高度可拖动)"
          className={cn(
            'h-6 rounded border px-1.5 text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent',
            managerOpen
              ? 'border-accent/30 bg-accent/20 font-medium text-accent'
              : 'border-transparent text-muted hover:bg-elevated hover:text-foreground',
          )}
        >
          指标{indicators.length > 0 ? ` ${indicators.length}` : ''}
        </button>
        {!hideOverlayToggles && (
          <button
            type="button"
            onClick={() => applyChips(!chipsOn)}
            disabled={minutePeriod}
            title={minutePeriod ? '筹码分布只在日线档有意义' : '筹码分布(成本分布): 右侧横条, 红=获利盘 / 绿=套牢盘'}
            className={cn(
              'h-6 rounded border px-1.5 text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent',
              chipsOn && !minutePeriod
                ? 'border-accent/30 bg-accent/20 font-medium text-accent'
                : 'border-transparent text-muted hover:bg-elevated hover:text-foreground',
              minutePeriod && 'cursor-not-allowed opacity-40',
            )}
          >
            筹码
          </button>
        )}
        {!hideOverlayToggles && (
          <button
            type="button"
            onClick={() => applySignals(!signalsOn)}
            disabled={period !== 'day'}
            title={
              period !== 'day'
                ? '策略信号(signal_*)是日K enriched 的列: 周/月线是聚合结果(只留 OHLCV), 分钟档压根没有'
                : signalsOn
                  ? '隐藏策略信号标记(买=红下三角 / 卖=绿上三角 / 双向=蓝)'
                  : '显示策略信号标记(买=红下三角 / 卖=绿上三角 / 双向=蓝)'
            }
            className={cn(
              'h-6 rounded border px-1.5 text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent',
              signalsOn && period === 'day'
                ? 'border-accent/30 bg-accent/20 font-medium text-accent'
                : 'border-transparent text-muted hover:bg-elevated hover:text-foreground',
              period !== 'day' && 'cursor-not-allowed opacity-40',
            )}
          >
            信号
          </button>
        )}
        {chipsOn && !minutePeriod && chips.data?.ok && (
          <span className="ml-1 text-[10px] text-muted" title="平均成本 / 获利盘比例">
            成本 {chips.data.avg_cost?.toFixed(2) ?? '—'} · 获利{' '}
            {chips.data.profit_ratio != null ? `${(chips.data.profit_ratio * 100).toFixed(1)}%` : '—'}
          </span>
        )}
        {minutePeriod && (
          <span className="ml-auto pr-1 text-[10px] text-muted/60" title="分钟K数据源: preagg=预聚合目录 / local=1m现场聚合">
            {period} · {active.data?.source ?? '…'}
          </span>
        )}
      </div>
      <div className="relative min-h-0 flex-1">
        {active.isLoading && (
          <div className="absolute inset-0 z-10 grid place-items-center bg-base/60 text-sm text-muted">加载 K 线…</div>
        )}
        {active.isError && (
          <div className="absolute inset-0 z-10 grid place-items-center bg-base/60 text-sm text-danger">K 线加载失败</div>
        )}
        {!active.isLoading && !active.isError && rows.length === 0 && (
          <div className="absolute inset-0 z-10 grid place-items-center bg-base/60 text-sm text-muted">
            暂无该周期数据
          </div>
        )}
        <div ref={containerRef} className="h-full w-full" />
        {managerOpen && (
          <IndicatorManager
            configs={indicators}
            onChange={setIndicators}
            onClose={() => setManagerOpen(false)}
          />
        )}
      </div>
    </div>
  )
}
