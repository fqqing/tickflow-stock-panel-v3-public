/**
 * 指数日 K 专用 klinecharts 组件(轻量)。
 *
 * ★ 为什么不复用 KLinePro: 指数走独立的 kline_index_* 数据源(api.indexDaily /
 *   api.indicesMarketDaily), 与个股的 api.klineDaily 分离; 且指数场景没有
 *   周期/复权/缠论/筹码/信号这些个股终端的语义, 用 KLinePro 会带出一堆错位的按钮。
 *   这里只做「行情浏览」: MA + 成交量 + MACD + 分时联动, 数据由页面传入(纯渲染)。
 */
import { useEffect, useMemo, useRef } from 'react'
import * as kc from 'klinecharts'
import type { KlineRow } from '@/lib/api'
import { useChartTheme } from '@/lib/theme'
import type { ChartPriceLine } from '@/lib/chart-primitives'
import { registerPriceLineOverlay } from './price-line-overlay'

const BULL = '#F04438' // 红涨
const BEAR = '#12B76A' // 绿跌
const CROSSHAIR = '#475569'
const CN_OFFSET_MS = 8 * 60 * 60 * 1000

/** 后端日期 -> 毫秒 epoch(与 KLinePro.parseTs 同判据) */
function parseTs(v: unknown): number | null {
  if (v == null) return null
  if (typeof v === 'number') return Number.isFinite(v) ? v : null
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?/.exec(String(v).trim())
  if (!m) return null
  const [, y, mo, d, hh, mi, ss] = m
  const ts = Date.UTC(+y, +mo - 1, +d, hh ? +hh : 0, mi ? +mi : 0, ss ? +ss : 0)
  if (!Number.isFinite(ts)) return null
  return (hh ? +hh : 0) < 8 ? ts : ts - CN_OFFSET_MS
}

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
  }
}

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

interface Props {
  symbol: string
  /** indexDaily 返回的日 K 行(纯渲染, 数据由页面拉) */
  rows: KlineRow[]
  height?: number
  /** 点击 K 线回调日期(分时联动) */
  onDateClick?: (date: string) => void
  /** 分时 hover 联动价格线 */
  linkedPrice?: number | null
}

export function IndexKLineChart({ symbol, rows, height = 620, onDateClick, linkedPrice }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<kc.Chart | null>(null)
  const rowsRef = useRef<kc.KLineData[]>([])
  const priceOverlayId = useRef<string | null>(null)
  const ct = useChartTheme()

  const data = useMemo(() => rows.map(rowToKLine).filter((d): d is kc.KLineData => d !== null), [rows])

  // 初始化图表(仅一次; 换指数重建)
  useEffect(() => {
    const el = containerRef.current
    if (!el || chartRef.current) return
    registerPriceLineOverlay()
    const chart = kc.init(el, { styles: buildStyles(ct) })
    if (!chart) return
    chartRef.current = chart
    priceOverlayId.current = null
    chart.setTimezone('Asia/Shanghai')
    chart.setDataLoader({
      getBars: ({ type, callback }) => {
        if (type === 'init') callback(rowsRef.current, { backward: false, forward: false })
        else callback([], false)
      },
    })
    chart.setSymbol({ ticker: symbol, pricePrecision: 2, volumePrecision: 0 })
    // 行情浏览三件套: MA(主图) + 成交量 + MACD(副图)
    chart.createIndicator({ name: 'MA', paneId: 'candle_pane' })
    chart.createIndicator({ name: 'VOL' })
    chart.createIndicator({ name: 'MACD' })
    return () => {
      kc.dispose(el)
      chartRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol])

  // 主题切换: 只换 styles, 不重建
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    chart.setStyles(buildStyles(ct))
  }, [ct])

  // 数据刷新
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    rowsRef.current = data
    chart.resetData()
  }, [data])

  // 点击 K 线 -> 回调日期(分时联动)
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !onDateClick) return
    const handler = (d?: unknown) => {
      const ts = (d as { timestamp?: number } | undefined)?.timestamp
      if (typeof ts === 'number') onDateClick(new Date(ts).toISOString().slice(0, 10))
    }
    chart.subscribeAction('onCandleBarClick', handler)
    return () => chart.unsubscribeAction('onCandleBarClick', handler)
  }, [onDateClick])

  // 分时 hover 联动价 -> 价位线
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const lines: ChartPriceLine[] = linkedPrice != null ? [{ value: linkedPrice, color: '#F79009' }] : []
    if (!priceOverlayId.current) {
      const first = rowsRef.current[0]
      if (!first) return
      const id = chart.createOverlay({
        name: 'priceLine',
        paneId: 'candle_pane',
        points: [{ timestamp: first.timestamp, value: first.close }],
        extendData: lines,
      })
      if (typeof id === 'string') priceOverlayId.current = id
    } else {
      chart.overrideOverlay({ id: priceOverlayId.current, extendData: lines })
    }
  }, [linkedPrice])

  return <div ref={containerRef} className="w-full" style={{ height }} />
}
