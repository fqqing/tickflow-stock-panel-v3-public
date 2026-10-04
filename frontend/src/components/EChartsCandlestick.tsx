import { useEffect, useRef, useCallback, useMemo, useState } from 'react'
import type { KlineRow } from '@/lib/api'
import { chartSession } from '@/lib/chartSession'
import { viewportToZoom, zoomToViewport } from '@/lib/chartViewport'
import { chartBars, chartFocus, chartSignals } from '@/lib/chartBridge'
import { findDateIndex, signalRowsToTimeline } from '@/lib/chart-timeline'
import { chartTheme, getTheme, useTheme } from '@/lib/theme'
import { densePolyline, POLYLINE_GAP } from '@/lib/chart-polyline'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import type { ChartMarker, ChartPolyline, ChartPriceLine, ChartRange } from '@/lib/chart-primitives'
import {
  collectSignalIds,
  SIGNAL_CN,
  SIGNAL_COLORS,
  signalKindOf,
} from './kline/signal-markers'

// 原语类型已抽到 lib/chart-primitives(换图表内核时上游无需改动), 此处原样导出保持兼容
export type { ChartMarker, ChartPolyline, ChartPriceLine, ChartRange }

/**
 * 解密公式派生列(趋势擒龙 / 资金动能 / 主图定量结构 / MACD 定量结构)。
 * 这些值由后端算好后随 K 线一起返回, 声明收敛到 `api.ts` 的 KlineRow ——
 * 新增一列只改三处(KlineRow / KLINE_CHART_FIELDS / StockDailyKChart.toOHLC),
 * 不要在这里再抄一遍: 此前两份声明各自漂移, 是"同一语义两处实现"的典型债。
 */
type FormulaFields = Pick<
  KlineRow,
  | 'td_signal' | 'td_a3' | 'cm_value'
  | 'st_dsg' | 'st_dxg' | 'st_csg' | 'st_cxg' | 'st_icon' | 'st_dn' | 'st_up'
  | 'ms_diff' | 'ms_dea' | 'ms_hist' | 'ms_btext' | 'ms_by' | 'ms_ttext' | 'ms_ty'
>

export interface OHLC extends FormulaFields {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume?: number
  ma5?: number | null
  ma10?: number | null
  ma20?: number | null
  ma60?: number | null
  macd_dif?: number | null
  macd_dea?: number | null
  macd_hist?: number | null
  rsi_6?: number | null
  rsi_14?: number | null
  rsi_24?: number | null
  kdj_k?: number | null
  kdj_d?: number | null
  kdj_j?: number | null
  boll_upper?: number | null
  boll_lower?: number | null
}

/**
 * 主图折线（用于缠论「笔」这类连续斜线）。
 *
 * 顶点按 date 升序给出，渲染时对相邻顶点之间的每个交易日做**线性插值**补点，
 * 因此画出来的是顶点之间严格笔直的折线（而不是各顶点独立连线的虚线）。
 * date 不在当前 x 轴范围内的顶点会被丢弃。
 */

export interface StockInfo {
  name?: string
  total_shares?: number
  float_shares?: number
  /** 扩展数据（key: configId__fieldName），来自 klineDaily 的 ext_columns */
  ext?: Record<string, unknown>
}

export interface VolumeCompareConfig {
  enabled: boolean
  days: number
}

interface SubChartContext {
  compact: boolean
  volumeCompare: VolumeCompareConfig
}

/** 子图定义 */
export interface SubChartDef {
  key: string
  label: string
  /** 子图固定高度 px */
  height: number
  /** 构建 series 数组 */
  buildSeries: (data: OHLC[], context: SubChartContext) => any[]
  /** 构建信息栏文字 (当前数据行 -> 显示内容) */
  buildInfo: (d: OHLC | null) => { label: string; color: string; value: string }[]
  /** Y 轴特殊配置 */
  yAxisConfig?: Record<string, any>
}

// ===== 成交量 N 日均量 =====
function volMaN(data: OHLC[], n: number): (number | null)[] {
  const result: (number | null)[] = []
  for (let i = 0; i < data.length; i++) {
    if (i < n - 1) { result.push(null); continue }
    let sum = 0
    for (let j = i - n + 1; j <= i; j++) sum += data[j].volume ?? 0
    result.push(sum / n)
  }
  return result
}

function fmtVol(v: number | null | undefined): string {
  if (v == null) return '—'
  if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿'
  if (v >= 1e4) return (v / 1e4).toFixed(0) + '万'
  return v.toFixed(0)
}

function volumeRatioAt(data: OHLC[], index: number, days: number): number | null {
  const window = Math.max(1, Math.min(20, Math.round(days)))
  if (index < window) return null
  let sum = 0
  for (let offset = 1; offset <= window; offset++) {
    const volume = data[index - offset]?.volume
    if (volume == null || !Number.isFinite(volume)) return null
    sum += volume
  }
  const average = sum / window
  const current = data[index]?.volume
  if (current == null || !Number.isFinite(current) || average <= 0) return null
  return current / average
}

function fmtVolumeRatio(value: number | null, digits = 2): string {
  return value == null ? '—' : `${value.toFixed(digits)}x`
}

export const SUB_CHARTS: SubChartDef[] = [
  {
    key: 'vol',
    label: '成交量',
    height: 84,
    yAxisConfig: { min: 0 },
    buildSeries: (data, context) => {
      const ma5Data = volMaN(data, 5)
      const ma10Data = volMaN(data, 10)
      const compareDays = context.volumeCompare.days
      return [
        {
          name: '成交量',
          type: 'bar',
          data: data.map((d, index) => {
            const ratio = volumeRatioAt(data, index, compareDays)
            return {
              value: d.volume ?? 0,
              volumeRatioLabel: ratio == null ? '' : fmtVolumeRatio(ratio, 1),
              itemStyle: {
                color: d.close >= d.open ? 'rgba(240,68,56,0.6)' : 'rgba(18,183,106,0.6)',
              },
            }
          }),
          barWidth: '60%',
          label: {
            show: context.volumeCompare.enabled && !context.compact,
            position: 'top',
            distance: 2,
            color: CT().text,
            fontSize: 8,
            fontFamily: 'JetBrains Mono, monospace',
            formatter: (params: any) => params.data?.volumeRatioLabel ?? '',
          },
          labelLayout: { hideOverlap: true },
          animation: false,
        },
        {
          name: 'VOL5',
          type: 'line',
          data: ma5Data,
          smooth: true, symbol: 'none', animation: false,
          lineStyle: { width: 1, color: '#FACC15' },
          itemStyle: { color: '#FACC15' },
        },
        {
          name: 'VOL10',
          type: 'line',
          data: ma10Data,
          smooth: true, symbol: 'none', animation: false,
          lineStyle: { width: 1, color: '#8B5CF6' },
          itemStyle: { color: '#8B5CF6' },
        },
      ]
    },
    buildInfo: (d) => {
      if (!d) return []
      return [
        { label: '量', color: d.close >= d.open ? '#C74040' : '#2D9B65', value: fmtVol(d.volume) },
      ]
    },
  },
  {
    key: 'macd',
    label: 'MACD',
    height: 72,
    buildSeries: (data) => [
      {
        name: 'DIF',
        type: 'line',
        data: data.map(d => d.macd_dif != null ? Number(d.macd_dif) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#FACC15' },
        itemStyle: { color: '#FACC15' },
      },
      {
        name: 'DEA',
        type: 'line',
        data: data.map(d => d.macd_dea != null ? Number(d.macd_dea) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#8B5CF6' },
        itemStyle: { color: '#8B5CF6' },
      },
      {
        name: 'MACD',
        type: 'bar',
        data: data.map(d => {
          const v = d.macd_hist
          if (v == null) return '-'
          return {
            value: Number(v),
            itemStyle: { color: Number(v) >= 0 ? 'rgba(240,68,56,0.6)' : 'rgba(18,183,106,0.6)' },
          }
        }),
        barWidth: '40%',
        animation: false,
      },
    ],
    buildInfo: (d) => {
      if (!d) return []
      return [
        { label: 'DIF', color: '#FACC15', value: d.macd_dif != null ? d.macd_dif.toFixed(3) : '—' },
        { label: 'DEA', color: '#8B5CF6', value: d.macd_dea != null ? d.macd_dea.toFixed(3) : '—' },
        { label: 'MACD', color: d.macd_hist != null && d.macd_hist >= 0 ? '#C74040' : '#2D9B65', value: d.macd_hist != null ? d.macd_hist.toFixed(3) : '—' },
      ]
    },
  },
  {
    key: 'momentum',
    label: '资金动能',
    height: 72,
    buildSeries: (data) => {
      const values = data.map(d => (d.cm_value != null ? Number(d.cm_value) : null))
      const positive = values.map(v => (v != null && v > 0 ? v : null))
      const negative = values.map(v => (v != null && v < 0 ? v : null))
      const momentumLine = (
        name: string,
        series: (number | null)[],
        color: string,
        fill: string,
        withThresholds: boolean,
      ) => ({
        name,
        type: 'line',
        data: series,
        symbol: 'none',
        animation: false,
        connectNulls: false,
        silent: true,
        lineStyle: { width: 1, color },
        itemStyle: { color },
        // origin: 0 → 面积在 0 轴与曲线之间填充（红上绿下，同同花顺口径）
        areaStyle: { color: fill, origin: 0 },
        // 阈值线只画一次, 否则两条 series 会各叠一份虚线
        markLine: withThresholds
          ? {
              silent: true,
              symbol: 'none',
              label: {
                show: true,
                position: 'insideEndTop',
                fontSize: 9,
                fontFamily: 'JetBrains Mono, monospace',
                formatter: (params: any) => params.name,
              },
              data: MOMENTUM_THRESHOLDS.map(t => ({
                yAxis: t.value,
                name: t.label,
                lineStyle: { color: t.color, type: 'dashed' as const, width: 1, opacity: 0.8 },
                label: { color: t.color },
              })),
            }
          : undefined,
      })
      return [
        momentumLine('资金动能', positive, '#F04438', 'rgba(240,68,56,0.45)', true),
        momentumLine('资金动能(负)', negative, '#12B76A', 'rgba(18,183,106,0.45)', false),
      ]
    },
    buildInfo: (d) => {
      if (!d || d.cm_value == null) {
        return [{ label: '资金动能', color: '#8E8E96', value: '—' }]
      }
      const value = Number(d.cm_value)
      const status = value >= 1.5 ? '极强' : value >= 0.5 ? '强' : value > 0 ? '偏强' : '弱'
      return [
        {
          label: '资金动能',
          color: value >= 0 ? '#C74040' : '#2D9B65',
          value: `${value.toFixed(2)} ${status}`,
        },
        { label: '强', color: '#FACC15', value: '0.50' },
        { label: '极强', color: '#8B5CF6', value: '1.50' },
      ]
    },
  },
  {
    key: 'macd_struct',
    label: 'MACD定量结构',
    height: 88,
    buildSeries: (data) => {
      // 结构标注用 1px 的透明散点承载, 文字通过 label 画在 DIFF 的缩放位置
      const markSeries = (
        name: string,
        textKey: 'ms_btext' | 'ms_ttext',
        yKey: 'ms_by' | 'ms_ty',
        position: 'bottom' | 'top',
      ) => ({
        name,
        type: 'scatter',
        symbol: 'rect',
        symbolSize: 1,
        itemStyle: { color: 'transparent' },
        animation: false,
        silent: true,
        z: 8,
        label: {
          show: true,
          position,
          distance: 2,
          fontSize: 10,
          fontWeight: 'bold' as const,
          fontFamily: 'JetBrains Mono, monospace',
          formatter: (params: any) => params.data?.mark ?? '',
        },
        data: data.flatMap(d => {
          const text = Number(d[textKey] ?? 0)
          const y = d[yKey]
          if (!text || y == null) return []
          return [{
            value: [d.date, Number(y)],
            mark: STRUCTURE_MARK_LABELS[text] ?? '',
            label: { color: STRUCTURE_MARK_COLORS[text] ?? CT().text },
          }]
        }),
      })
      return [
        {
          name: 'DIF',
          type: 'line',
          data: data.map(d => (d.ms_diff != null ? Number(d.ms_diff) : '-')),
          smooth: true, symbol: 'none', animation: false,
          lineStyle: { width: 1, color: '#FACC15' },
          itemStyle: { color: '#FACC15' },
        },
        {
          name: 'DEA',
          type: 'line',
          data: data.map(d => (d.ms_dea != null ? Number(d.ms_dea) : '-')),
          smooth: true, symbol: 'none', animation: false,
          lineStyle: { width: 1, color: '#8B5CF6' },
          itemStyle: { color: '#8B5CF6' },
        },
        {
          name: 'MACD',
          type: 'bar',
          data: data.map(d => {
            const v = d.ms_hist
            if (v == null) return '-'
            return {
              value: Number(v),
              itemStyle: { color: Number(v) >= 0 ? 'rgba(240,68,56,0.6)' : 'rgba(18,183,106,0.6)' },
            }
          }),
          barWidth: '40%',
          animation: false,
        },
        markSeries('底部结构', 'ms_btext', 'ms_by', 'bottom'),
        markSeries('顶部结构', 'ms_ttext', 'ms_ty', 'top'),
      ]
    },
    buildInfo: (d) => {
      if (!d) return []
      const markText = (value?: number | null) => {
        const code = Number(value ?? 0)
        return code ? (STRUCTURE_MARK_LABELS[code] ?? '—') : '—'
      }
      const markColor = (value?: number | null) => STRUCTURE_MARK_COLORS[Number(value ?? 0)] ?? '#8E8E96'
      return [
        { label: 'DIF', color: '#FACC15', value: d.ms_diff != null ? Number(d.ms_diff).toFixed(3) : '—' },
        { label: 'DEA', color: '#8B5CF6', value: d.ms_dea != null ? Number(d.ms_dea).toFixed(3) : '—' },
        {
          label: 'MACD',
          color: d.ms_hist != null && Number(d.ms_hist) >= 0 ? '#C74040' : '#2D9B65',
          value: d.ms_hist != null ? Number(d.ms_hist).toFixed(3) : '—',
        },
        { label: '底结构', color: markColor(d.ms_btext), value: markText(d.ms_btext) },
        { label: '顶结构', color: markColor(d.ms_ttext), value: markText(d.ms_ttext) },
      ]
    },
  },
  {
    key: 'rsi',
    label: 'RSI',
    height: 72,
    yAxisConfig: { min: 0, max: 100 },
    buildSeries: (data) => [
      {
        name: 'RSI6',
        type: 'line',
        data: data.map(d => d.rsi_6 != null ? Number(d.rsi_6) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#FACC15' },
        itemStyle: { color: '#FACC15' },
      },
      {
        name: 'RSI14',
        type: 'line',
        data: data.map(d => d.rsi_14 != null ? Number(d.rsi_14) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#3B82F6' },
        itemStyle: { color: '#3B82F6' },
      },
      {
        name: 'RSI24',
        type: 'line',
        data: data.map(d => d.rsi_24 != null ? Number(d.rsi_24) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#8B5CF6' },
        itemStyle: { color: '#8B5CF6' },
      },
    ],
    buildInfo: (d) => {
      if (!d) return []
      return [
        { label: 'RSI6', color: '#FACC15', value: d.rsi_6 != null ? d.rsi_6.toFixed(1) : '—' },
        { label: 'RSI14', color: '#3B82F6', value: d.rsi_14 != null ? d.rsi_14.toFixed(1) : '—' },
        { label: 'RSI24', color: '#8B5CF6', value: d.rsi_24 != null ? d.rsi_24.toFixed(1) : '—' },
      ]
    },
  },
  {
    key: 'kdj',
    label: 'KDJ',
    height: 72,
    buildSeries: (data) => [
      {
        name: 'K',
        type: 'line',
        data: data.map(d => d.kdj_k != null ? Number(d.kdj_k) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#FACC15' },
        itemStyle: { color: '#FACC15' },
      },
      {
        name: 'D',
        type: 'line',
        data: data.map(d => d.kdj_d != null ? Number(d.kdj_d) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#3B82F6' },
        itemStyle: { color: '#3B82F6' },
      },
      {
        name: 'J',
        type: 'line',
        data: data.map(d => d.kdj_j != null ? Number(d.kdj_j) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#8B5CF6' },
        itemStyle: { color: '#8B5CF6' },
      },
    ],
    buildInfo: (d) => {
      if (!d) return []
      return [
        { label: 'K', color: '#FACC15', value: d.kdj_k != null ? d.kdj_k.toFixed(1) : '—' },
        { label: 'D', color: '#3B82F6', value: d.kdj_d != null ? d.kdj_d.toFixed(1) : '—' },
        { label: 'J', color: '#8B5CF6', value: d.kdj_j != null ? d.kdj_j.toFixed(1) : '—' },
      ]
    },
  },
]

/** 向后兼容的 INDICATORS 导出 (不含 vol) */
export const INDICATORS = SUB_CHARTS.filter(s => s.key !== 'vol')

/** 主图叠加指标 (画在 K 线上方, 不占副图空间) */
export const OVERLAY_INDICATORS: { key: string; label: string }[] = [
  { key: 'boll', label: 'BOLL' },
  { key: 'tdragon', label: '蛟龙出海' },
  { key: 'structure', label: '主图定量结构' },
]

interface Props {
  data: OHLC[]
  markers?: ChartMarker[]
  ranges?: ChartRange[]
  priceLines?: ChartPriceLine[]
  /** 主图折线（缠论笔等连续斜线） */
  polylines?: ChartPolyline[]
  height?: number
  showMA?: boolean
  showInfoBar?: boolean
  showMarkers?: boolean
  onToggleMarkers?: () => void
  /**
   * 策略信号标记(与 KLinePro 侧同一套口径: 买=红 / 卖=绿 / 双向=蓝)。
   * 默认关。要真的画出来, data 里必须带 signal_* 列 —— 由上层在 fields 里
   * 加上(见 api.ts 的 klineChartFields), 列没下发时这里自然什么都不画。
   */
  signalsEnabled?: boolean
  stockInfo?: StockInfo
  symbol?: string
  linkedPrice?: number | null
  onDateClick?: (date: string) => void
  onPriceDoubleClick?: (price: number, currentPrice: number) => void
  /**
   * 图表实例就绪回调(含重建后再次就绪), 供父组件挂载 zrender 事件(如画线工具)。
   * 组件卸载时不会回调, 调用方需自行在 chart 上解绑。
   */
  onChartReady?: (chart: ECharts) => void
  /** 默认可见蜡烛根数, 默认 60 */
  visibleBars?: number
  /** 已激活的子图 key 列表 (含 vol, 按点击顺序) */
  activeIndicators?: string[]
  /** 成交量柱相对前 N 个交易日均量的显示设置 */
  volumeCompare?: VolumeCompareConfig
}

// 序列颜色 (双主题通用); 画布轴/网格/文字等主题相关色走 CT() 动态取
const THEME = {
  bull: '#C74040',
  bear: '#2D9B65',
  bullAlpha: 'rgba(240,68,56,0.7)',
  bearAlpha: 'rgba(18,183,106,0.7)',
  ma5: '#A1A1AA',
  ma10: '#3B82F6',
  ma20: '#F97316',
  ma60: '#8B5CF6',
  bg: 'transparent',
}

/** 当前主题的图表调色板 (buildOption/信息栏在渲染时调用; 主题切换由组件 effect 触发重建)。 */
const CT = () => chartTheme(getTheme())

/** 可见蜡烛超过此数量时，涨停/炸板标签切换为小圆点。 */
const COMPACT_THRESHOLD = 60

/** 资金动能强弱阈值 (同同花顺口径: 0.5 强 / 1.5 极强)。 */
const MOMENTUM_THRESHOLDS = [
  { value: 0.5, label: '强', color: '#FACC15' },
  { value: 1.5, label: '极强', color: '#8B5CF6' },
]

/** 蛟龙出海: 生命线(MA10)与信号箭头用色。 */
const DRAGON_LINE_COLOR = '#F04438'
const DRAGON_SIGNAL_COLOR = '#FACC15'

/**
 * 主图定量结构: 短轨道 EMA(HIGH/LOW,25) 用红/绿, 长轨道 EMA(HIGH/LOW,89) 用洋红/蓝,
 * 与通达信原公式的 COLORRED / COLORGREEN / COLORMAGENTA / COLORBLUE 一致。
 */
const STRUCTURE_SHORT_UP = '#F04438'
const STRUCTURE_SHORT_DOWN = '#12B76A'
const STRUCTURE_LONG_UP = '#C026D3'
const STRUCTURE_LONG_DOWN = '#2563EB'
/** 轨道带填充 (DRAWBAND): 收盘在带上轨之上 / 在带内 / 在下轨之下 */
const STRUCTURE_BAND_FILL: Record<string, string> = {
  up: 'rgba(240,68,56,0.10)',
  flat: 'rgba(148,163,184,0.10)',
  down: 'rgba(18,183,106,0.10)',
}
/** 九转数字: 下跌九转标红(低位买点), 上涨九转标绿(高位卖点), 同原公式 DRAWTEXT 配色。 */
const STRUCTURE_DN_COLOR = '#F04438'
const STRUCTURE_UP_COLOR = '#12B76A'
/** 结构标注文案与配色: 1 结构形成 / 2 钝化 / 3 钝化消失 */
const STRUCTURE_MARK_LABELS: Record<number, string> = {
  1: '结构形成',
  2: '钝化',
  3: '消失',
}
const STRUCTURE_MARK_COLORS: Record<number, string> = {
  1: '#F04438',
  2: '#F59E0B',
  3: '#8E8E96',
}

/** 子图上方信息栏高度 (px) */
const INFO_BAR_H = 16
/** 子图之间的间距 (px) */
const SUB_GAP_PX = 4
/** 主图最小高度 (px) —— 拖拽时不可再压 */
const MIN_MAIN_H = 120
/** 单个副图最小图表高度 (px) */
const MIN_SUB_H = 40
/** 拖拽分隔条命中高度 (px) */
const DIVIDER_HIT_PX = 6
/** 主图上方留白 (px) */
const TOP_PAD = 8
/** 主图下方留给 x 轴标签的高度 (px) */
const CANDLE_BOTTOM_PAD = 22
/** 用户拖拽过的窗格高度持久化 key（跨股票/跨会话保留） */
const PANE_H_STORAGE_KEY = 'tickflow.klinePaneHeights'

/** 窗格高度布局: 主图 + 各副图(顺序与 activeSubDefs 一致) */
interface PaneLayout {
  mainH: number
  subH: number[]
}

function loadPaneHeights(): Record<string, number> {
  try {
    const raw = localStorage.getItem(PANE_H_STORAGE_KEY)
    const parsed = raw ? JSON.parse(raw) : null
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, number>) : {}
  } catch {
    return {}
  }
}

function savePaneHeights(value: Record<string, number>): void {
  try {
    localStorage.setItem(PANE_H_STORAGE_KEY, JSON.stringify(value))
  } catch {
    // localStorage 不可用(隐私模式/配额)时静默降级为内存态
  }
}

/**
 * 解析窗格高度: 用户拖过的优先, 否则取默认; 总高超出容器时先等比压副图, 再压主图。
 *
 * ⚠️ 关键修正: 旧实现把 subTotalH 直接加进 chartHeight(把容器撑高), 副图一多就
 *    溢出父容器被裁掉 —— 也就是「副图全挤在最下方看不见」。这里改成容器高度固定,
 *    所有窗格只在容器内部分配, 永不溢出。
 */
function resolvePaneHeights(
  keys: string[],
  defaults: number[],
  avail: number,
  stored: Record<string, number>,
): PaneLayout {
  const n = keys.length
  const fixed = n * (INFO_BAR_H + SUB_GAP_PX)
  let subH = keys.map((k, i) => Math.max(MIN_SUB_H, stored[k] ?? defaults[i]))
  const subSum = () => subH.reduce((acc, h) => acc + h, 0)
  let mainH = stored['main'] ?? 0

  // 没拖过主图: 主图吃掉剩余空间
  if (!mainH) mainH = Math.max(avail - fixed - subSum(), MIN_MAIN_H)

  let total = mainH + subSum() + fixed
  if (total > avail) {
    // 1) 副图等比压缩(但不低于 MIN_SUB_H)
    const room = Math.max(subSum() - (total - avail), n * MIN_SUB_H)
    const scale = room / Math.max(subSum(), 1)
    subH = subH.map(h => Math.max(MIN_SUB_H, Math.floor(h * scale)))
    total = mainH + subSum() + fixed
    // 2) 仍超出: 压主图
    if (total > avail) mainH = Math.max(MIN_MAIN_H, avail - fixed - subSum())
  }
  return { mainH, subH }
}

/** 主图定量结构的信息栏片段 (双轨数值 + 交叉图标 + 九转数字)。 */
function structureInfoParts(d: OHLC | null, active: boolean): string[] {
  if (!d || !active) return []
  const parts: string[] = []
  const pushTrack = (label: string, value: number | null | undefined, color: string) => {
    if (value == null) return
    parts.push(`<span style="color:${color}">${label}:${Number(value).toFixed(2)}</span>`)
  }
  pushTrack('短上轨', d.st_dsg, STRUCTURE_SHORT_UP)
  pushTrack('短下轨', d.st_dxg, STRUCTURE_SHORT_DOWN)
  pushTrack('长上轨', d.st_csg, STRUCTURE_LONG_UP)
  pushTrack('长下轨', d.st_cxg, STRUCTURE_LONG_DOWN)
  if (d.st_icon) {
    const breakout = Number(d.st_icon) === 4
    parts.push(
      `<span style="color:${breakout ? STRUCTURE_SHORT_UP : STRUCTURE_SHORT_DOWN}">` +
        `${breakout ? 'BBB 上穿' : 'SSS 下破'}</span>`,
    )
  }
  if (d.st_dn) parts.push(`<span style="color:${STRUCTURE_DN_COLOR}">低九:${d.st_dn}</span>`)
  if (d.st_up) parts.push(`<span style="color:${STRUCTURE_UP_COLOR}">高九:${d.st_up}</span>`)
  return parts
}

function buildSubInfoGraphics(
  data: OHLC[],
  infoIdx: number,
  activeIndicators: string[],
  subStartTop: number,
  volumeCompare: VolumeCompareConfig,
  subHeights?: number[],
): any[] {
  const d = infoIdx >= 0 && infoIdx < data.length ? data[infoIdx] : null
  const graphics: any[] = []
  let curTop = subStartTop
  // 副图序号: 用于取用户拖拽后的实际高度(activeIndicators 里可能混着主图叠加项)
  let si = 0

  activeIndicators.forEach((key) => {
    const def = SUB_CHARTS.find(s => s.key === key)
    if (!def) return
    const paneH = subHeights?.[si] ?? def.height
    si += 1

    const items = def.buildInfo(d)
    if (def.key === 'vol' && d) {
      const calcVolMa = (n: number) => {
        if (infoIdx < n - 1) return null
        let sum = 0
        for (let j = infoIdx - n + 1; j <= infoIdx; j++) sum += data[j].volume ?? 0
        return sum / n
      }
      const vol5 = calcVolMa(5)
      const vol10 = calcVolMa(10)
      items.push({ label: 'VOL5', color: '#FACC15', value: fmtVol(vol5) })
      items.push({ label: 'VOL10', color: '#8B5CF6', value: fmtVol(vol10) })
      if (volumeCompare.enabled) {
        const ratio = volumeRatioAt(data, infoIdx, volumeCompare.days)
        items.push({
          label: `量比${volumeCompare.days}`,
          color: ratio != null && ratio >= 1 ? '#C74040' : '#2D9B65',
          value: fmtVolumeRatio(ratio),
        })
      }
    }

    // 每个元素加固定 id，确保 ECharts 增量更新时能正确匹配
    graphics.push({
      id: `sub-sep-${key}`,
      type: 'line',
      shape: { x1: 0, y1: curTop, x2: 2000, y2: curTop },
      style: { stroke: 'rgba(255,255,255,0.08)', lineWidth: 1 },
      silent: true, z: 0,
    })
    graphics.push({
      id: `sub-label-${key}`,
      type: 'text',
      style: {
        text: def.label,
        x: 4, y: curTop + 4,
        fill: '#8E8E96',
        fontSize: 10, fontFamily: 'JetBrains Mono, monospace',
        fontWeight: 'bold',
      },
      silent: true, z: 10,
    })

    const richTextParts: string[] = []
    const rich: Record<string, any> = {}
    items.forEach((item, idx) => {
      const styleKey = `s${idx}`
      richTextParts.push(`{${styleKey}|${item.label}:${item.value}}`)
      rich[styleKey] = {
        fill: item.color,
        fontSize: 10,
        fontFamily: 'JetBrains Mono, monospace',
      }
    })
    graphics.push({
      id: `sub-val-${key}`,
      type: 'text',
      right: 24,
      style: {
        text: richTextParts.join(`{gap|  }`),
        y: curTop + 3,
        rich: {
          gap: { fill: 'transparent', fontSize: 10 },
          ...rich,
        },
        fontSize: 10,
        fontFamily: 'JetBrains Mono, monospace',
        textAlign: 'right',
        textVerticalAlign: 'top',
      },
      silent: true, z: 10,
    })

    curTop += INFO_BAR_H + paneH + SUB_GAP_PX
  })

  return graphics
}

function buildOption(
  data: OHLC[],
  dates: string[],
  dateIndexMap: Map<string, number>,
  markers: ChartMarker[] | undefined,
  ranges: ChartRange[] | undefined,
  priceLines: ChartPriceLine[] | undefined,
  polylines: ChartPolyline[] | undefined,
  showMA: boolean,
  compact: boolean,
  activeIndicators: string[],
  layout: PaneLayout,
  infoIdx: number,
  linkedPrice: number | null | undefined,
  volumeCompare: VolumeCompareConfig,
): EChartsOption {
  const candleData = data.map(d => [d.open, d.close, d.low, d.high])

  const hasMA = showMA && data.some(d => d.ma5 != null || d.ma10 != null || d.ma20 != null || d.ma60 != null)

  const markPointData: any[] = []
  if (markers && markers.length > 0) {
    for (const m of markers) {
      const idx = dateIndexMap.get(m.date)
      if (idx == null) continue
      const d = data[idx]
      const isBuy = m.kind === 'buy'
      const isSell = m.kind === 'sell'

      if (m.above) {
        const dotColor = m.color ?? (isBuy ? '#FACC15' : CT().text)
        if (compact) {
          markPointData.push({
            name: m.date, coord: [m.date, d.high],
            symbol: 'circle', symbolSize: 4, symbolOffset: [0, -10],
            itemStyle: { color: dotColor, cursor: 'pointer' },
            label: { show: false }, z: 100, zlevel: 10,
          })
        } else {
          markPointData.push({
            name: m.date, coord: [m.date, d.high],
            symbol: 'circle', symbolSize: 12, symbolOffset: [0, -2],
            itemStyle: { color: 'transparent' },
            label: {
              show: true, formatter: m.label ?? '', position: 'top', distance: 0,
              color: dotColor, fontSize: 10, fontWeight: 'normal',
              fontFamily: 'JetBrains Mono, monospace',
            },
            z: 100, zlevel: 10,
          })
        }
      } else {
        markPointData.push({
          name: m.label ?? '',
          coord: [m.date, isBuy ? d.low : d.high],
          symbol: 'arrow', symbolSize: 12,
          symbolRotate: isBuy ? 0 : 180,
          symbolOffset: isBuy ? [0, '60%'] : [0, '-60%'],
          itemStyle: { color: isBuy ? THEME.bull : isSell ? THEME.bear : CT().text },
          label: {
            show: !!m.label, formatter: m.label ?? '',
            position: isBuy ? 'bottom' : 'top', distance: 8,
            color: CT().text, fontSize: 10,
            fontFamily: 'JetBrains Mono, monospace',
          },
        })
      }
    }
  }

  // 蛟龙出海（趋势擒龙）: 信号日箭头画在 low 下方, 点击可定位到该日
  const showDragon = activeIndicators.includes('tdragon')
  if (showDragon) {
    for (const d of data) {
      if (!d.td_signal) continue
      markPointData.push({
        name: d.date,
        coord: [d.date, d.low],
        symbol: 'arrow',
        symbolSize: compact ? 9 : 14,
        symbolRotate: 0,
        symbolOffset: [0, compact ? '45%' : '70%'],
        itemStyle: { color: DRAGON_SIGNAL_COLOR, cursor: 'pointer' },
        label: { show: false },
        z: 99,
        zlevel: 10,
      })
    }
  }

  // ====== 布局计算 ======
  const left = 60
  const right = 20
  const topPad = TOP_PAD
  const candleBottomPad = CANDLE_BOTTOM_PAD

  const activeSubDefs: SubChartDef[] = []
  activeIndicators.forEach(key => {
    const def = SUB_CHARTS.find(s => s.key === key)
    if (!def) return
    activeSubDefs.push(def)
  })

  // 主图/副图高度由上层按「可拖拽布局」算好传入; 这里只消费, 不再自己撑高容器
  const candleAvail = Math.max(layout.mainH, MIN_MAIN_H)

  const grids: any[] = []
  const xAxes: any[] = []
  const yAxes: any[] = []
  const series: any[] = []
  const xAxisIndices: number[] = []

  const priceLineValues = (priceLines ?? [])
    .map(line => line.value)
    .filter(value => Number.isFinite(value) && value > 0)
  const axisMin = priceLineValues.length > 0
    ? ({ min, max }: { min: number; max: number }) => {
        const nextMin = Math.min(min, ...priceLineValues)
        const nextMax = Math.max(max, ...priceLineValues)
        return nextMin - Math.max((nextMax - nextMin) * 0.03, nextMax * 0.001)
      }
    : undefined
  const axisMax = priceLineValues.length > 0
    ? ({ min, max }: { min: number; max: number }) => {
        const nextMin = Math.min(min, ...priceLineValues)
        const nextMax = Math.max(max, ...priceLineValues)
        return nextMax + Math.max((nextMax - nextMin) * 0.03, nextMax * 0.001)
      }
    : undefined

  // ===== grid 0: K线主图 =====
  grids.push({ left, right, top: topPad, height: candleAvail })
  xAxes.push({
    type: 'category', data: dates, boundaryGap: true,
    axisLine: { lineStyle: { color: CT().border } },
    axisLabel: { color: CT().text, fontSize: 10, fontFamily: 'JetBrains Mono, monospace' },
    axisTick: { show: false },
    splitLine: { show: false },
  })
  yAxes.push({
    scale: true,
    min: axisMin,
    max: axisMax,
    // 上下各留 3% 边距: 防止最高/最低点的蜡烛贴边, 涨停/炸板标签被遮挡
    boundaryGap: [0.03, 0.03],
    splitArea: { show: false },
    axisLine: { show: false }, axisTick: { show: false },
    splitLine: { lineStyle: { color: CT().grid } },
    axisLabel: { color: CT().text, fontSize: 10, fontFamily: 'JetBrains Mono, monospace' },
  })
  xAxisIndices.push(0)

  const markAreaData = (ranges ?? [])
    .filter(r => dateIndexMap.has(r.start) && dateIndexMap.has(r.end))
    .map(r => ([
      {
        name: r.label ?? '',
        xAxis: r.start,
        itemStyle: { color: r.color ?? 'rgba(59,130,246,0.08)' },
        label: {
          show: !!r.label,
          position: 'insideTop',
          distance: 8,
          color: CT().tooltipText,
          backgroundColor: CT().tooltipBg,
          borderColor: 'rgba(59,130,246,0.35)',
          borderWidth: 1,
          borderRadius: 4,
          padding: [2, 6],
          fontSize: 10,
          fontFamily: 'JetBrains Mono, monospace',
        },
      },
      { xAxis: r.end },
    ]))

  const markLineData: any[] = (priceLines ?? [])
    .filter(line => Number.isFinite(line.value))
    .map(line => {
      const lineStyle = {
        color: line.color ?? CT().text,
        type: 'dashed' as const,
        width: 1,
        opacity: 0.92,
      }
      const label = {
        show: !!line.label,
        formatter: line.label ?? '',
        position: 'insideEndTop' as const,
        color: line.color ?? CT().text,
        backgroundColor: CT().tooltipBg,
        borderRadius: 4,
        padding: [2, 6],
        fontSize: 10,
        fontFamily: 'JetBrains Mono, monospace',
      }
      if (line.start && line.end && dateIndexMap.has(line.start) && dateIndexMap.has(line.end)) {
        return [
          { xAxis: line.start, yAxis: line.value },
          { xAxis: line.end, yAxis: line.value, lineStyle, label, symbol: 'none' },
        ]
      }
      return { yAxis: line.value, lineStyle, label, symbol: 'none' }
    })

  if (linkedPrice != null) {
    markLineData.push({
      yAxis: linkedPrice,
      lineStyle: { color: '#3B82F6', type: 'dashed', width: 1, opacity: 0.7 },
      label: {
        show: true,
        formatter: linkedPrice.toFixed(2),
        position: 'insideEndTop',
        color: '#3B82F6',
        fontSize: 10,
        fontFamily: 'JetBrains Mono, monospace',
        backgroundColor: CT().tooltipBg,
        borderColor: '#3B82F6',
        borderWidth: 1,
        padding: [1, 4],
        borderRadius: 2,
      },
      symbol: 'none',
    })
  }

  series.push({
    name: 'K', type: 'candlestick', data: candleData,
    animation: false,
    itemStyle: {
      color: THEME.bull, color0: THEME.bear,
      borderColor: THEME.bull, borderColor0: THEME.bear,
      cursor: 'pointer',
    },
    markPoint: markPointData.length > 0 ? { data: markPointData, animation: false } : undefined,
    markArea: markAreaData.length > 0 ? { silent: true, data: markAreaData } : undefined,
    markLine: markLineData.length > 0 ? { silent: true, symbol: 'none', data: markLineData, animation: false } : undefined,
  })

  if (hasMA) {
    const maLine = (key: keyof OHLC, color: string, name: string) => ({
      name, type: 'line',
      data: data.map(d => (d[key] != null ? Number(d[key]) : '-')),
      smooth: true, symbol: 'none', animation: false,
      silent: true,
      lineStyle: { width: 1, color }, itemStyle: { color },
    })
    series.push(maLine('ma5', THEME.ma5, 'MA5'))
    series.push(maLine('ma10', THEME.ma10, 'MA10'))
    series.push(maLine('ma20', THEME.ma20, 'MA20'))
    series.push(maLine('ma60', THEME.ma60, 'MA60'))
  }

  // 主图折线（缠论「笔」）：相邻顶点之间逐 bar 线性插值 ⇒ 画出来是笔直的斜线。
  for (const poly of polylines ?? []) {
    if (poly.points.length < 2) continue
    const dense = densePolyline(poly.points, dates.length, dateIndexMap)
    // 只有落在图内且连成段的顶点才有意义；没有两个有效顶点时 dense 全是占位符
    if (dense.every(v => v === POLYLINE_GAP)) continue
    const lineColor = poly.color ?? CT().text
    series.push({
      name: poly.name ?? 'chan-stroke',
      type: 'line',
      symbol: poly.showSymbol ? 'circle' : 'none',
      symbolSize: 3,
      animation: false,
      silent: true,
      connectNulls: false,
      data: dense,
      lineStyle: {
        width: poly.width ?? 1.2,
        color: lineColor,
        type: poly.dashed ? ('dashed' as const) : ('solid' as const),
        opacity: 0.95,
      },
      itemStyle: { color: lineColor },
      z: 7,
    })
  }

  // BOLL 布林带 — 需在 activeIndicators 中激活
  const showBOLL = activeIndicators.includes('boll') && data.some(d => d.boll_upper != null || d.boll_lower != null)
  if (showBOLL) {
    const bollLine = (key: keyof OHLC, color: string, name: string) => ({
      name, type: 'line',
      data: data.map(d => (d[key] != null ? Number(d[key]) : '-')),
      smooth: true, symbol: 'none', animation: false,
      silent: true,
      lineStyle: { width: 1, color, type: 'dashed' as const }, itemStyle: { color },
    })
    series.push(bollLine('boll_upper', '#E879F9', 'BOLL上'))
    series.push(bollLine('boll_lower', '#E879F9', 'BOLL下'))
  }

  // 蛟龙出海: 生命线 (MA10) — 与常规 MA10 并存, 但更醒目
  if (showDragon) {
    series.push({
      name: '生命线',
      type: 'line',
      data: data.map(d => (d.ma10 != null ? Number(d.ma10) : '-')),
      smooth: true, symbol: 'none', animation: false,
      silent: true,
      lineStyle: { width: 1.6, color: DRAGON_LINE_COLOR },
      itemStyle: { color: DRAGON_LINE_COLOR },
      z: 5,
    })
  }

  // ===== 主图定量结构 (EMA25/89 双轨 + 交叉图标 + 九转数字) =====
  const showStructure = activeIndicators.includes('structure') && data.some(d => d.st_dsg != null)
  if (showStructure) {
    // DRAWBAND: 逐 bar 画矩形填充上下轨之间, 填充色随收盘价相对轨道的位置变化
    const bandSeries = (
      name: string,
      upperKey: 'st_dsg' | 'st_csg',
      lowerKey: 'st_dxg' | 'st_cxg',
    ) => ({
      name,
      type: 'custom',
      silent: true,
      animation: false,
      z: 1,
      renderItem: (_params: any, api: any) => {
        const index = api.value(0)
        const upper = api.coord([index, api.value(1)])
        const lower = api.coord([index, api.value(2)])
        const half = Math.max((api.size([1, 0])[0] || 6) / 2, 1)
        return {
          type: 'rect',
          shape: {
            x: upper[0] - half,
            y: Math.min(upper[1], lower[1]),
            width: half * 2,
            height: Math.max(Math.abs(upper[1] - lower[1]), 0.5),
          },
          style: { fill: api.value(3) },
        }
      },
      data: data.flatMap((d, index) => {
        const upper = d[upperKey]
        const lower = d[lowerKey]
        if (upper == null || lower == null) return []
        const close = Number(d.close)
        const state = close > Number(upper) ? 'up' : close < Number(lower) ? 'down' : 'flat'
        return [[index, Number(upper), Number(lower), STRUCTURE_BAND_FILL[state]]]
      }),
    })

    // 变色双轨: 原公式用 IF(C > X, X, DRAWNULL) + IF(C < X, X, DRAWNULL) 两条互补线
    const trackLine = (
      name: string,
      key: 'st_dsg' | 'st_dxg' | 'st_csg' | 'st_cxg',
      color: string,
      above: boolean,
      width: number,
    ) => ({
      name,
      type: 'line',
      symbol: 'none',
      animation: false,
      silent: true,
      connectNulls: false,
      data: data.map(d => {
        const value = d[key]
        if (value == null) return '-'
        return (Number(d.close) > Number(value)) === above ? Number(value) : '-'
      }),
      lineStyle: { width, color },
      itemStyle: { color },
      z: 6,
    })

    series.push(bandSeries('短轨道带', 'st_dsg', 'st_dxg'))
    series.push(bandSeries('长轨道带', 'st_csg', 'st_cxg'))
    series.push(trackLine('短上轨', 'st_dsg', STRUCTURE_SHORT_UP, true, 1.6))
    series.push(trackLine('短上轨(跌)', 'st_dsg', STRUCTURE_SHORT_DOWN, false, 1.2))
    series.push(trackLine('短下轨', 'st_dxg', STRUCTURE_SHORT_UP, true, 1.6))
    series.push(trackLine('短下轨(跌)', 'st_dxg', STRUCTURE_SHORT_DOWN, false, 1.2))
    series.push(trackLine('长上轨', 'st_csg', STRUCTURE_LONG_UP, true, 1.4))
    series.push(trackLine('长上轨(跌)', 'st_csg', STRUCTURE_LONG_DOWN, false, 1.1))
    series.push(trackLine('长下轨', 'st_cxg', STRUCTURE_LONG_UP, true, 1.4))
    series.push(trackLine('长下轨(跌)', 'st_cxg', STRUCTURE_LONG_DOWN, false, 1.1))

    // BBB/SSS 交叉图标: 4 底部向上箭头 (画在 LOW 下方) / 5 顶部向下箭头 (画在 HIGH 上方)
    const iconSeries = (name: string, icon: number, color: string, above: boolean) => ({
      name,
      type: 'scatter',
      silent: true,
      animation: false,
      z: 9,
      symbol: 'arrow',
      symbolSize: compact ? 8 : 12,
      symbolRotate: above ? 180 : 0,
      symbolOffset: above ? [0, '-60%'] : [0, '60%'],
      itemStyle: { color },
      label: { show: false },
      data: data.flatMap(d =>
        Number(d.st_icon ?? 0) === icon
          ? [[d.date, above ? Number(d.high) : Number(d.low)]]
          : []
      ),
    })
    series.push(iconSeries('结构上穿', 4, STRUCTURE_SHORT_UP, false))
    series.push(iconSeries('结构下破', 5, STRUCTURE_SHORT_DOWN, true))

    // 九转数字: 下跌九转 6~9 标 LOW 下方 (红), 上涨九转 6~9 标 HIGH 上方 (绿)
    const digitSeries = (name: string, color: string, above: boolean) => ({
      name,
      type: 'scatter',
      silent: true,
      animation: false,
      z: 9,
      symbol: 'rect',
      symbolSize: 1,
      itemStyle: { color: 'transparent' },
      label: {
        show: true,
        position: above ? 'top' : 'bottom',
        distance: 2,
        color,
        fontSize: 10,
        fontWeight: 'bold' as const,
        fontFamily: 'JetBrains Mono, monospace',
        formatter: (params: any) => params.data?.mark ?? '',
      },
      data: data.flatMap(d => {
        const digit = above ? Number(d.st_up ?? 0) : Number(d.st_dn ?? 0)
        if (!digit) return []
        return [{ value: [d.date, above ? Number(d.high) : Number(d.low)], mark: String(digit) }]
      }),
    })
    series.push(digitSeries('下跌九转', STRUCTURE_DN_COLOR, false))
    series.push(digitSeries('上涨九转', STRUCTURE_UP_COLOR, true))
  }

  // ===== 子图区域 =====
  let curTop = topPad + candleAvail + candleBottomPad

  activeSubDefs.forEach((def, i) => {
    const gridIdx = i + 1
    const xAxisIdx = i + 1
    const yAxisIdx = i + 1

    const chartTop = curTop + INFO_BAR_H
    const paneH = layout.subH[i] ?? def.height
    grids.push({
      left, right,
      top: chartTop,
      height: paneH,
      show: true,
      borderColor: CT().grid,
      borderWidth: 1,
    })

    xAxes.push({
      type: 'category', gridIndex: gridIdx, data: dates, boundaryGap: true,
      axisLine: { show: false }, axisLabel: { show: false },
      axisTick: { show: false }, splitLine: { show: false },
      axisPointer: { label: { show: false } },
    })

    const isFixedRange = !!def.yAxisConfig
    yAxes.push({
      scale: !isFixedRange,
      ...(isFixedRange ? def.yAxisConfig : {}),
      gridIndex: gridIdx,
      splitNumber: 2,
      axisLine: { show: false }, axisTick: { show: false },
      splitLine: { lineStyle: { color: CT().grid } },
      axisLabel: {
        show: true, color: CT().text, fontSize: 9,
        fontFamily: 'JetBrains Mono, monospace',
      },
    })

    xAxisIndices.push(xAxisIdx)

    const subSeries = def.buildSeries(data, { compact, volumeCompare })
    subSeries.forEach((s: any) => {
      series.push({ ...s, xAxisIndex: xAxisIdx, yAxisIndex: yAxisIdx })
    })

    curTop += INFO_BAR_H + paneH + SUB_GAP_PX
  })

  // 子图信息栏 graphic
  const subStartTop = topPad + candleAvail + candleBottomPad
  const infoGraphics = buildSubInfoGraphics(data, infoIdx, activeIndicators, subStartTop, volumeCompare, layout.subH)

  return {
    animation: false,
    backgroundColor: THEME.bg,
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross', crossStyle: { color: CT().crosshair } },
      backgroundColor: 'transparent',
      borderWidth: 0,
      textStyle: { fontSize: 0 },
      formatter: () => '',
    },
    axisPointer: {
      link: [{ xAxisIndex: 'all' }],
      label: {
        backgroundColor: CT().crosshairLabelBg,
        fontFamily: 'JetBrains Mono, monospace',
        fontSize: 10,
      },
    },
    graphic: infoGraphics.length > 0 ? infoGraphics : undefined,
    grid: grids,
    xAxis: xAxes,
    yAxis: yAxes,
    dataZoom: [
      {
        type: 'inside',
        xAxisIndex: xAxisIndices,
        start: 0,
        end: 100,
        moveOnMouseMove: true,
        zoomOnMouseWheel: true,
      },
    ],
    series,
  }
}

export function EChartsCandlestick({
  data,
  markers,
  ranges,
  priceLines,
  polylines,
  height = 480,
  showMA = true,
  showInfoBar = true,
  showMarkers: showMarkersProp = true,
  onToggleMarkers: _onToggleMarkers,
  signalsEnabled = false,
  stockInfo,
  symbol: _symbol,
  linkedPrice,
  onDateClick,
  onPriceDoubleClick,
  onChartReady,
  visibleBars = 60,
  activeIndicators = [],
  volumeCompare = { enabled: true, days: 1 },
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<ECharts | null>(null)
  const dataRef = useRef(data)
  dataRef.current = data
  const onDateClickRef = useRef(onDateClick)
  onDateClickRef.current = onDateClick
  const onPriceDoubleClickRef = useRef(onPriceDoubleClick)
  onPriceDoubleClickRef.current = onPriceDoubleClick
  const onChartReadyRef = useRef(onChartReady)
  onChartReadyRef.current = onChartReady
  // 主题: buildOption/信息栏内部通过 CT() 动态取调色板, 这里只负责切换时触发重建
  const theme = useTheme()

  // --- 全部用 ref，避免高频交互触发 React 重渲染 ---
  const infoIdxRef = useRef<number>(data.length - 1)
  const compactRef = useRef(false)
  const userZoomRef = useRef<{ start: number; end: number } | null>(null)

  // 需要在闭包中访问最新值的变量 — 先声明占位，后面赋值
  const activeIndicatorsRef = useRef(activeIndicators)
  activeIndicatorsRef.current = activeIndicators
  const volumeCompareRef = useRef(volumeCompare)
  volumeCompareRef.current = volumeCompare
  const subStartTopRef = useRef(0)
  const layoutRef = useRef<PaneLayout>({ mainH: 300, subH: [] })
  const getInfoBarHTMLRef = useRef<() => string>(() => '')

  // 强制刷新信息栏 DOM 的回调
  const infoBarRef = useRef<HTMLDivElement>(null)
  const triggerInfoBarUpdate = useRef(() => {
    const idx = infoIdxRef.current
    const curData = dataRef.current
    const d = idx >= 0 && idx < curData.length ? curData[idx] : null
    if (!d) return
    const chart = chartRef.current
    if (!chart) return
    const infoGraphics = buildSubInfoGraphics(
      curData,
      idx,
      activeIndicatorsRef.current,
      subStartTopRef.current,
      volumeCompareRef.current,
      layoutRef.current.subH,
    )
    if (infoGraphics.length > 0) {
      chart.setOption({ graphic: infoGraphics }, { lazyUpdate: true })
    }
  }).current

  // 副图定义(顺序 = 图上从上到下)
  const activeSubDefs = activeIndicators
    .map(key => SUB_CHARTS.find(s => s.key === key))
    .filter((d): d is SubChartDef => !!d)

  const mainInfoBarH = showInfoBar ? 40 : 0

  // ⚠️ 容器高度固定 = 可用高度, 不再把副图总高加进来。
  //    旧实现 `Math.max(..., 8+120+14+subTotalH)` 会把容器撑高、溢出父容器,
  //    于是副图一多就被裁在下面看不见 —— 也就是「副图全挤在最下方」。
  const chartHeight = Math.max(height - mainInfoBarH, TOP_PAD + MIN_MAIN_H + 14)

  // 窗格高度: 用户拖过的值持久化(localStorage), 空间不足时自动等比压缩 ⇒ 永不溢出
  const [storedH, setStoredH] = useState<Record<string, number>>(() => loadPaneHeights())
  const [dragIdx, setDragIdx] = useState<number | null>(null)
  const layout = useMemo(() => {
    const defs = activeIndicators
      .map(key => SUB_CHARTS.find(s => s.key === key))
      .filter((d): d is SubChartDef => !!d)
    return resolvePaneHeights(
      defs.map(d => d.key),
      defs.map(d => d.height),
      chartHeight - TOP_PAD - CANDLE_BOTTOM_PAD,
      storedH,
    )
  }, [activeIndicators, chartHeight, storedH])
  layoutRef.current = layout
  // 副图区起始 y = 顶部留白 + 主图高 + x 轴标签高度
  subStartTopRef.current = TOP_PAD + layout.mainH + CANDLE_BOTTOM_PAD

  // 分隔条位置: dividers[i] 位于「窗格 i」与「窗格 i+1」之间(窗格 0 = 主图)
  const dividers = useMemo(() => {
    const out: { key: string; y: number }[] = []
    let y = TOP_PAD + layout.mainH + CANDLE_BOTTOM_PAD
    activeSubDefs.forEach((def, i) => {
      out.push({ key: def.key, y })
      y += INFO_BAR_H + (layout.subH[i] ?? def.height) + SUB_GAP_PX
    })
    return out
  }, [layout, activeSubDefs])

  const startDrag = useCallback((idx: number, event: { preventDefault: () => void; clientY: number }) => {
    event.preventDefault()
    const startY = event.clientY
    const heights = [layout.mainH, ...layout.subH]
    const a0 = heights[idx]
    const b0 = heights[idx + 1]
    const aMin = idx === 0 ? MIN_MAIN_H : MIN_SUB_H
    const bMin = MIN_SUB_H
    const keys = ['main', ...activeSubDefs.map(d => d.key)]
    setDragIdx(idx)

    const onMove = (ev: MouseEvent) => {
      let a = a0 + (ev.clientY - startY)
      let b = b0 - (ev.clientY - startY)
      // 触底时把多出来的量让给另一侧, 保证总高不变
      if (a < aMin) { b -= aMin - a; a = aMin }
      if (b < bMin) { a -= bMin - b; b = bMin }
      setStoredH(prev => {
        const next = {
          ...prev,
          [keys[idx]]: Math.max(aMin, Math.round(a)),
          [keys[idx + 1]]: Math.max(bMin, Math.round(b)),
        }
        savePaneHeights(next)
        return next
      })
    }
    const onUp = () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      setDragIdx(null)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }, [layout, activeSubDefs])

  // 预计算 date→index Map (O(1) 查找)
  const dates = useMemo(() => data.map(d => d.date), [data])
  const dateIndexMap = useMemo(() => {
    const m = new Map<string, number>()
    dates.forEach((d, i) => m.set(d, i))
    return m
  }, [dates])

  // 计算 dataZoom 初始范围
  const initialZoom = useMemo(() => ({
    start: Math.max(0, 100 - (visibleBars / Math.max(data.length, 1)) * 100),
    end: 100,
  }), [visibleBars, data.length])

  // ===== 信息栏 HTML 内容 (基于 infoIdxRef.current) =====
  const getInfoBarHTML = useCallback(() => {
    let idx = infoIdxRef.current
    let d = idx >= 0 && idx < data.length ? data[idx] : null
    // fallback: 如果当前 idx 无数据，取最后一根 K 线
    if (!d && data.length > 0) {
      idx = data.length - 1
      d = data[idx]
    }
    if (!d) return ''
    const prev = idx > 0 ? data[idx - 1] : null
    const chg = prev ? d.close - prev.close : 0
    const isUp = chg >= 0
    const clr = isUp ? THEME.bull : THEME.bear
    const floatShares = stockInfo?.float_shares
    const turnoverRate = floatShares && d.volume ? (d.volume * 100 / floatShares * 100) : null

    let html = `<div style="display:flex;align-items:center;gap:6px;padding:0 8px;font:11px 'JetBrains Mono',monospace;select:none;height:20px;flex-wrap:wrap">`
    html += `<span style="color:${CT().text}">${d.date}</span>`
    html += `<span style="color:${CT().text}">开</span>`
    html += `<span style="color:${d.open >= d.close ? THEME.bear : THEME.bull}">${d.open.toFixed(2)}</span>`
    html += `<span style="color:${CT().text}">高</span>`
    html += `<span style="color:${THEME.bull}">${d.high.toFixed(2)}</span>`
    html += `<span style="color:${CT().text}">低</span>`
    html += `<span style="color:${THEME.bear}">${d.low.toFixed(2)}</span>`
    html += `<span style="color:${CT().text}">收</span>`
    html += `<span style="color:${clr};font-weight:600">${d.close.toFixed(2)}</span>`
    // 涨跌幅 (收盘后, 换手前; 和收间隔一些距离)
    if (prev) {
      const chgPct = (chg / prev.close * 100)
      html += `<span style="color:${clr};margin-left:8px">${isUp ? '+' : ''}${chgPct.toFixed(2)}%</span>`
    }
    if (turnoverRate != null) {
      html += `<span style="color:${CT().text}">换手</span>`
      html += `<span style="color:${CT().text}">${turnoverRate.toFixed(2)}%</span>`
    }
    html += `</div>`

    // 第二行: MA + BOLL + 蛟龙出海 (信号 / 生命线 / 距 9 连阳天数)
    const dragonParts: string[] = []
    if (activeIndicators.includes('tdragon')) {
      dragonParts.push(`<span style="color:${DRAGON_SIGNAL_COLOR}">蛟龙出海:${d.td_signal ? '信号' : '—'}</span>`)
      if (d.td_a3 != null) {
        dragonParts.push(`<span style="color:${CT().text}">距9连阳:${d.td_a3}</span>`)
      }
      if (d.ma10 != null) {
        dragonParts.push(`<span style="color:${DRAGON_LINE_COLOR}">生命线:${Number(d.ma10).toFixed(2)}</span>`)
      }
    }
    const structureParts = structureInfoParts(d, activeIndicators.includes('structure'))
    if (showMA || dragonParts.length > 0 || structureParts.length > 0) {
      html += `<div style="display:flex;align-items:center;gap:10px;padding:0 8px;font:11px 'JetBrains Mono',monospace;select:none;height:20px;flex-wrap:wrap">`
      if (showMA) {
        if (d.ma5 != null) html += `<span style="color:${THEME.ma5}">MA5:${Number(d.ma5).toFixed(2)}</span>`
        if (d.ma10 != null) html += `<span style="color:${THEME.ma10}">MA10:${Number(d.ma10).toFixed(2)}</span>`
        if (d.ma20 != null) html += `<span style="color:${THEME.ma20}">MA20:${Number(d.ma20).toFixed(2)}</span>`
        if (d.ma60 != null) html += `<span style="color:${THEME.ma60}">MA60:${Number(d.ma60).toFixed(2)}</span>`
        if (d.boll_upper != null && activeIndicators.includes('boll')) {
          html += `<span style="color:#E879F9">BOLL:${Number(d.boll_upper).toFixed(2)}/${Number(d.ma20).toFixed(2)}/${Number(d.boll_lower).toFixed(2)}</span>`
        }
      }
      html += dragonParts.join('')
      html += structureParts.join('')
      html += `</div>`
    }

    return html
  }, [data, stockInfo, showMA, activeIndicators])
  getInfoBarHTMLRef.current = getInfoBarHTML

  // data 变化时重置 infoIdx
  useEffect(() => {
    infoIdxRef.current = data.length - 1
    compactRef.current = false
    userZoomRef.current = null
  }, [data.length])

  // ===== 初始化 chart (只在 chartHeight 变化时重建) =====
  useEffect(() => {
    const el = containerRef.current
    if (!el) return

    const chart = echarts.init(el, undefined, { renderer: 'canvas' })
    chartRef.current = chart
    onChartReadyRef.current?.(chart)

    // 鼠标移动 → 只更新 ref + DOM，不触发 React re-render
    // 设计原则: 找不到有效数据时保持上次显示，永远不清空信息栏
    chart.on('updateAxisPointer', (event: any) => {
      const axesInfo = event.axesInfo
      if (!axesInfo) return // 鼠标移出图表区域，保持当前显示
      for (const info of Object.values(axesInfo)) {
        const val = (info as any)?.value
        if (val == null) continue
        const d = dataRef.current
        const idx = typeof val === 'number' ? val : d.findIndex(x => x.date === val)
        if (idx >= 0 && idx < d.length) {
          if (infoIdxRef.current === idx) return
          infoIdxRef.current = idx

          // 直接更新信息栏 DOM (通过 ref 读取最新的生成函数)
          const infoEl = infoBarRef.current
          if (infoEl) {
            const html = getInfoBarHTMLRef.current()
            if (html) infoEl.innerHTML = html  // 只在有内容时更新
          }

          // 更新子图 graphic
          triggerInfoBarUpdate()
          return
        }
      }
      // 没有找到有效数据 — 不做任何操作，保持上次显示
    })

    chart.on('click', (params: any) => {
      if (params.componentType === 'markPoint' && params.name) {
        onDateClickRef.current?.(params.name)
        return
      }
      if (params.seriesName !== 'K' || params.dataIndex == null) return
      const d = dataRef.current
      const idx = params.dataIndex
      if (idx >= 0 && idx < d.length) {
        onDateClickRef.current?.(d[idx].date)
      }
    })

    const handlePriceDoubleClick = (event: { offsetX: number; offsetY: number }) => {
      const pixel: [number, number] = [event.offsetX, event.offsetY]
      if (!chart.containPixel({ gridIndex: 0 }, pixel)) return
      const coordinate = chart.convertFromPixel({ xAxisIndex: 0, yAxisIndex: 0 }, pixel)
      const price = Array.isArray(coordinate) ? Number(coordinate[1]) : NaN
      const currentPrice = dataRef.current[dataRef.current.length - 1]?.close
      if (Number.isFinite(price) && price > 0 && Number.isFinite(currentPrice) && currentPrice > 0) {
        onPriceDoubleClickRef.current?.(price, currentPrice)
      }
    }
    chart.getZr().on('dblclick', handlePriceDoubleClick)

    // dataZoom → 只更新 ref，不触发 React re-render
    // compact 变化时需要增量更新 markPoint
    chart.on('dataZoom', () => {
      const opt = chart.getOption() as any
      const zoom = opt?.dataZoom?.[0]
      if (!zoom) return
      userZoomRef.current = { start: zoom.start, end: zoom.end }

      const d = dataRef.current
      const total = d.length
      // 写回会话(静默通道, 不触发 React 重渲染): 切到另一个内核时由它重放
      const vp = zoomToViewport({ start: zoom.start, end: zoom.end }, total)
      if (vp) chartSession.setViewport(vp)
      const visibleCount = Math.round(total * (zoom.end - zoom.start) / 100)
      const newCompact = visibleCount > COMPACT_THRESHOLD
      if (newCompact !== compactRef.current) {
        compactRef.current = newCompact
        updateCompactPresentation()
      }
    })

    const ro = new ResizeObserver(() => { chart.resize() })
    ro.observe(el)

    return () => {
      chart.off('updateAxisPointer')
      chart.off('click')
      chart.off('dataZoom')
      chart.getZr().off('dblclick', handlePriceDoubleClick)
      ro.disconnect()
      chart.dispose()
      chartRef.current = null
    }
  }, [chartHeight]) // eslint-disable-line react-hooks/exhaustive-deps

  // 缩放跨过紧凑阈值时，仅增量更新标签，不重建整张图。
  function updateCompactPresentation() {
    const chart = chartRef.current
    if (!chart) return
    const mkrs = showMarkersProp ? markers : undefined
    const compact = compactRef.current
    const seriesUpdates: any[] = []
    const markPointData: any[] = []
    for (const m of mkrs ?? []) {
      const idx = dateIndexMap.get(m.date)
      if (idx == null) continue
      const d = data[idx]
      const isBuy = m.kind === 'buy'
      const isSell = m.kind === 'sell'
      if (m.above) {
        const dotColor = m.color ?? (isBuy ? '#FACC15' : CT().text)
        if (compact) {
          markPointData.push({
            name: m.date, coord: [m.date, d.high],
            symbol: 'circle', symbolSize: 4, symbolOffset: [0, -10],
            itemStyle: { color: dotColor, cursor: 'pointer' },
            label: { show: false }, z: 100, zlevel: 10,
          })
        } else {
          markPointData.push({
            name: m.date, coord: [m.date, d.high],
            symbol: 'circle', symbolSize: 12, symbolOffset: [0, -2],
            itemStyle: { color: 'transparent' },
            label: {
              show: true, formatter: m.label ?? '', position: 'top', distance: 0,
              color: dotColor, fontSize: 10, fontWeight: 'normal',
              fontFamily: 'JetBrains Mono, monospace',
            },
            z: 100, zlevel: 10,
          })
        }
      } else {
        markPointData.push({
          name: m.label ?? '',
          coord: [m.date, isBuy ? d.low : d.high],
          symbol: 'arrow', symbolSize: 12,
          symbolRotate: isBuy ? 0 : 180,
          symbolOffset: isBuy ? [0, '60%'] : [0, '-60%'],
          itemStyle: { color: isBuy ? THEME.bull : isSell ? THEME.bear : CT().text },
          label: {
            show: !!m.label, formatter: m.label ?? '',
            position: isBuy ? 'bottom' : 'top', distance: 8,
            color: CT().text, fontSize: 10,
            fontFamily: 'JetBrains Mono, monospace',
          },
        })
      }
    }
    if (mkrs?.length) {
      seriesUpdates.push({
        name: 'K',
        markPoint: markPointData.length > 0 ? { data: markPointData, animation: false } : undefined,
      })
    }
    if (activeIndicatorsRef.current.includes('vol')) {
      seriesUpdates.push({
        name: '成交量',
        label: { show: volumeCompareRef.current.enabled && !compact },
      })
    }
    if (seriesUpdates.length > 0) chart.setOption({ series: seriesUpdates })
  }

  // ── 策略信号标记: 与 KLinePro 侧同一套口径(共用 collectSignalIds/中文名/配色) ──
  // ECharts 的 markPoint 没法像 klinecharts overlay 那样按槽位堆叠, 所以一根 K 线
  // 触发多个信号时合并成一个标记: 方向取优先级(卖 > 买 > 双向), 标签用「/」连前两个。
  const signalMarkers = useMemo<ChartMarker[]>(() => {
    if (!signalsEnabled) return []
    const out: ChartMarker[] = []
    for (const d of data) {
      const ids = collectSignalIds(d as unknown as Record<string, unknown>)
      if (ids.length === 0) continue
      const kinds = ids.map(signalKindOf)
      const hasExit = kinds.includes('exit')
      const hasEntry = kinds.includes('entry')
      const names = ids.slice(0, 2).map((id) => SIGNAL_CN.get(id) ?? id)
      out.push({
        date: d.date,
        kind: hasExit ? 'sell' : hasEntry ? 'buy' : 'neutral',
        label: names.join('/') + (ids.length > 2 ? '…' : ''),
        color: SIGNAL_COLORS[hasExit ? 'exit' : hasEntry ? 'entry' : 'both'],
      })
    }
    return out
  }, [data, signalsEnabled])

  // ===== 核心: 仅在数据/配置变更时全量 setOption =====
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return

    // 缠论 markers 与信号标记是两个独立开关, 合并后一起下发
    const allMarkers = [
      ...(showMarkersProp ? markers ?? [] : []),
      ...signalMarkers,
    ]

    const option = buildOption(
      data, dates, dateIndexMap,
      allMarkers.length > 0 ? allMarkers : undefined,
      ranges,
      priceLines,
      polylines,
      showMA, compactRef.current,
      activeIndicators, layout,
      infoIdxRef.current,
      linkedPrice,
      volumeCompare,
    )

    if (dragIdx !== null) {
      // 拖拽调整窗格高度时只提交 grid + 信息栏位置, 不重建 series
      // —— 每帧全量 setOption 会明显卡顿
      chart.setOption({ grid: option.grid, graphic: option.graphic }, { lazyUpdate: true })
      return
    }

    chart.setOption(option, true)

    // 恢复缩放位置: 会话视口 > 本轮用户缩放 > 默认区间。
    // 会话视口是从上一个内核带过来的(互译见 chartViewport), 优先它 —— 这是
    // 「切内核不丢缩放」的关键; 之后用户滚动会经 dataZoom 事件把它覆盖掉。
    const sessionVp = chartSession.getViewport()
    const zoom = (sessionVp ? viewportToZoom(sessionVp, data.length) : null)
      ?? userZoomRef.current
      ?? initialZoom
    chart.dispatchAction({ type: 'dataZoom', start: zoom.start, end: zoom.end })

    // 初始信息栏
    const infoEl = infoBarRef.current
    if (infoEl) {
      infoEl.innerHTML = getInfoBarHTML()
    }
  }, [data, markers, ranges, priceLines, polylines, linkedPrice, showMA, showMarkersProp, activeIndicators, volumeCompare, layout, dragIdx, dates, dateIndexMap, initialZoom, getInfoBarHTML, theme])

  // ── 事件时间轴(P4-1): 上报日期序列与信号事件 ────────────────────
  // 与 KLinePro 侧同一套口径(都调 signalRowsToTimeline), 时间轴不管当前是哪个
  // 内核, 拿到的都是同一份数据。
  const signalTimeline = useMemo(
    () => (signalsEnabled ? signalRowsToTimeline(data) : []),
    [signalsEnabled, data],
  )
  useEffect(() => { chartBars.set(dates) }, [dates])
  useEffect(() => { chartSignals.set(signalTimeline) }, [signalTimeline])

  // ── 点击定位(P4-2): 时间轴点某天 → dataZoom 挪过去 ──────────────
  // 百分比换算复用 viewportToZoom, 不在这里另算一套 —— 否则「切内核丢缩放」
  // 那套互译就等于有了第二个实现。dispatch 后 ECharts 会自己发 dataZoom 事件,
  // 会话视口随之更新, 切到另一个内核时定位结果照样保留。
  useEffect(() => chartFocus.subscribe((req) => {
    const chart = chartRef.current
    const d = chartBars.get()
    if (!chart || d.length === 0) return
    const idx = findDateIndex(d, req.date)
    if (idx < 0) return
    const total = d.length
    const vp = chartSession.getViewport()
    let vis = vp && vp.visibleBars > 0
      ? vp.visibleBars
      : null
    if (vis == null) {
      const z = userZoomRef.current ?? initialZoom
      vis = Math.round(total * (z.end - z.start) / 100)
    }
    vis = Math.min(total, Math.max(1, vis))
    const from = Math.min(
      Math.max(0, idx - Math.round((vis - 1) * req.anchor)),
      Math.max(0, total - vis),
    )
    const zoom = viewportToZoom({ visibleBars: vis, offsetRight: total - from - vis }, total)
    chart.dispatchAction({ type: 'dataZoom', start: zoom.start, end: zoom.end })
    chartFocus.consume()
  }), [data, dates]) // eslint-disable-line react-hooks/exhaustive-deps

  // 渲染信息栏容器 (内容由 JS 直接写入)
  const initialHTML = useMemo(() => {
    const idx = data.length - 1
    const d = idx >= 0 && idx < data.length ? data[idx] : null
    if (!d) return ''
    const floatShares = stockInfo?.float_shares
    const turnoverRate = floatShares && d.volume ? (d.volume * 100 / floatShares * 100) : null
    let html = `<div style="display:flex;align-items:center;gap:6px;padding:0 8px;font:11px 'JetBrains Mono',monospace;height:20px;flex-wrap:wrap">`
    html += `<span style="color:${CT().text}">${d.date}</span>`
    html += `<span style="color:${CT().text}">开</span>`
    html += `<span style="color:${d.open >= d.close ? THEME.bear : THEME.bull}">${d.open.toFixed(2)}</span>`
    html += `<span style="color:${CT().text}">高</span>`
    html += `<span style="color:${THEME.bull}">${d.high.toFixed(2)}</span>`
    html += `<span style="color:${CT().text}">低</span>`
    html += `<span style="color:${THEME.bear}">${d.low.toFixed(2)}</span>`
    html += `<span style="color:${CT().text}">收</span>`
    const prevClose0 = data[idx-1]?.close ?? d.close
    const clr0 = d.close >= prevClose0 ? THEME.bull : THEME.bear
    html += `<span style="color:${clr0};font-weight:600">${d.close.toFixed(2)}</span>`
    // 涨跌幅 (收盘后, 换手前; 和收间隔一些距离)
    if (idx > 0) {
      const chgPct0 = ((d.close - prevClose0) / prevClose0 * 100)
      html += `<span style="color:${clr0};margin-left:8px">${chgPct0 >= 0 ? '+' : ''}${chgPct0.toFixed(2)}%</span>`
    }
    if (turnoverRate != null) {
      html += `<span style="color:${CT().text}">换手</span>`
      html += `<span style="color:${CT().text}">${turnoverRate.toFixed(2)}%</span>`
    }
    html += `</div>`
    const dragonParts0: string[] = []
    if (activeIndicators.includes('tdragon')) {
      dragonParts0.push(`<span style="color:${DRAGON_SIGNAL_COLOR}">蛟龙出海:${d.td_signal ? '信号' : '—'}</span>`)
      if (d.td_a3 != null) {
        dragonParts0.push(`<span style="color:${CT().text}">距9连阳:${d.td_a3}</span>`)
      }
      if (d.ma10 != null) {
        dragonParts0.push(`<span style="color:${DRAGON_LINE_COLOR}">生命线:${Number(d.ma10).toFixed(2)}</span>`)
      }
    }
    const structureParts0 = structureInfoParts(d, activeIndicators.includes('structure'))
    if (showMA || dragonParts0.length > 0 || structureParts0.length > 0) {
      html += `<div style="display:flex;align-items:center;gap:10px;padding:0 8px;font:11px 'JetBrains Mono',monospace;height:20px;flex-wrap:wrap">`
      if (showMA) {
        if (d.ma5 != null) html += `<span style="color:${THEME.ma5}">MA5:${Number(d.ma5).toFixed(2)}</span>`
        if (d.ma10 != null) html += `<span style="color:${THEME.ma10}">MA10:${Number(d.ma10).toFixed(2)}</span>`
        if (d.ma20 != null) html += `<span style="color:${THEME.ma20}">MA20:${Number(d.ma20).toFixed(2)}</span>`
        if (d.ma60 != null) html += `<span style="color:${THEME.ma60}">MA60:${Number(d.ma60).toFixed(2)}</span>`
        if (d.boll_upper != null && activeIndicators.includes('boll')) {
          html += `<span style="color:#E879F9">BOLL:${Number(d.boll_upper).toFixed(2)}/${Number(d.ma20).toFixed(2)}/${Number(d.boll_lower).toFixed(2)}</span>`
        }
      }
      html += dragonParts0.join('')
      html += structureParts0.join('')
      html += `</div>`
    }
    return html
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="w-full">
      {/* 主图信息栏 — 内容由 JS 直接操作 innerHTML */}
      {showInfoBar && (
        <div ref={infoBarRef} style={{ backgroundColor: CT().infoBarBg }}
          dangerouslySetInnerHTML={{ __html: initialHTML }} />
      )}

      {/* ECharts canvas + 可拖拽分隔条 */}
      <div className="relative w-full" style={{ height: chartHeight }}>
        <div ref={containerRef} className="h-full w-full" />
        {dividers.map((d, i) => (
          <div
            key={d.key}
            role="separator"
            aria-orientation="horizontal"
            title="上下拖动调整窗格高度"
            onMouseDown={event => startDrag(i, event)}
            className={`absolute inset-x-0 z-10 cursor-row-resize transition-colors ${
              dragIdx === i ? 'bg-accent/60' : 'bg-transparent hover:bg-accent/30'
            }`}
            style={{ top: d.y, height: DIVIDER_HIT_PX, transform: 'translateY(-50%)' }}
          />
        ))}
      </div>
    </div>
  )
}
