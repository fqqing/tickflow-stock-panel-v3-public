/**
 * 图表会话层(ChartSession) —— 「切内核」之所以割裂, 根因是状态住在组件树里。
 *
 * ★ 问题: 此前 symbol / period / 复权 / 结构开关提升到了终端层, 但视口(缩放与
 *   滚动位置)、指标清单、叠加层 id 全住在各自渲染组件内部。终端层是三元条件
 *   渲染(useKLine ? <KLinePro/> : <StockPanel/>), 切换即整棵子树卸载重建,
 *   这些状态必然陪葬 —— 表现就是「切一下内核, 图缩回默认区间」。
 *
 * ★ 解法: 把真正决定「我看到的是哪张图」的状态提到组件树之外的会话里。
 *   渲染器只负责把会话重放到自己的画布上, 切换内核 = 换渲染器 + 重放。
 *
 * ★ 为什么视口不进 React 状态: 图表滚动/缩放每秒可触发几十次, 若走 setState
 *   会把整棵终端子树重渲染, 交互直接卡死。所以 viewport 走**静默通道** ——
 *   渲染器随时写回, 但只在「新渲染器挂载」那一刻被读走一次。
 */
import { useSyncExternalStore } from 'react'
import type { KLineAdjust, KLinePeriod } from '@/lib/klinePeriod'

/**
 * 视口的跨内核表达: **可见 bar 数 + 右侧还剩几根**。
 * 不用像素/日期区间是因为两个内核的缩放语义不同 —— klinecharts 按 barSpace(像素)
 * 缩放, ECharts 按 dataZoom 百分比。唯有「多少根 + 右边留几根」是两边都能互译的。
 */
export interface ChartViewport {
  visibleBars: number
  offsetRight: number
}

/** 与会话共享的主图叠加层开关 */
export interface ChartOverlays {
  /** 主图定量结构(EMA25/89 双轨 + 九转) */
  structure: boolean
  /** 缠论笔 / 中枢 / 买卖点 */
  chan: boolean
  /** 筹码分布 */
  chips: boolean
  /**
   * 策略信号标记。只有日线档有意义 —— signal_* 是 enriched 日K的列, 周/月线是
   * 聚合结果(聚合只保留 OHLCV), 分钟档根本没有这些列。
   */
  signals: boolean
  /**
   * 监控触发标记。数据来自 alerts.jsonl (近 7 天), 只在日线档画。
   * 默认关: 不是每只票都有触发记录, 常开会在图上留一堆无意义的空查询。
   */
  alerts: boolean
  /** 回测买卖点标记。数据来自最近一次策略回测的落盘, 同样只在日线档画 */
  trades: boolean
  /**
   * 事件时间轴。它不是画在 K 线上的叠加层, 而是图下方那条「事件索引条」:
   * 把信号 / 触发 / 买卖点按日期排开, 点一下视口就跳过去。
   * ★ 它显示的是**当前已启用的那几类事件** —— 时间轴上的点与图上的标记永远
   *   一一对应, 不会出现「条上有、图上没有」的错位。所以打开它会连带开 signals。
   */
  timeline: boolean
}

export interface ChartSessionState {
  symbol: string
  period: KLinePeriod
  adjust: KLineAdjust
  overlays: ChartOverlays
}

const DEFAULT_STATE: ChartSessionState = {
  symbol: '',
  period: 'day',
  adjust: 'qfq',
  overlays: {
    structure: true, chan: false, chips: false,
    signals: false, alerts: false, trades: false, timeline: false,
  },
}

let state: ChartSessionState = DEFAULT_STATE
let viewport: ChartViewport | null = null

type Listener = () => void
const listeners = new Set<Listener>()

function emit(): void {
  for (const l of Array.from(listeners)) l()
}

function patch(next: Partial<ChartSessionState>): void {
  let changed = false
  for (const k of Object.keys(next) as (keyof ChartSessionState)[]) {
    const v = next[k]
    if (v === undefined) continue
    if (v === state[k]) continue
    changed = true
  }
  if (!changed) return
  state = { ...state, ...next }
  emit()
}

export const chartSession = {
  getState: (): ChartSessionState => state,
  subscribe: (l: Listener): (() => void) => {
    listeners.add(l)
    return () => { listeners.delete(l) }
  },

  patch,

  setSymbol: (symbol: string) => patch({ symbol }),
  setPeriod: (period: KLinePeriod) => patch({ period }),
  setAdjust: (adjust: KLineAdjust) => patch({ adjust }),
  setOverlay: (key: keyof ChartOverlays, value: boolean) => {
    if (state.overlays[key] === value) return
    patch({ overlays: { ...state.overlays, [key]: value } })
  },

  // ── 视口静默通道 ──
  getViewport: (): ChartViewport | null => viewport,
  setViewport: (v: ChartViewport | null) => { viewport = v },
  /** 换股 / 换周期后旧视口已无意义, 由渲染器调用清空 */
  resetViewport: () => { viewport = null },
}

/**
 * 工作区模板: 把散落的开关收成「场景」。
 * 此前周期/复权/叠加层是十几个各自独立的开关, 每次换场景都要挨个点;
 * 现在一键落到一组预设上, 而且模板是**会话级**的 —— 切内核不丢, 与渲染器无关。
 */
export interface WorkspacePreset {
  id: string
  label: string
  hint: string
  period: KLinePeriod
  adjust: KLineAdjust
  overlays: ChartOverlays
}

export const WORKSPACE_PRESETS: WorkspacePreset[] = [
  {
    id: 'technical',
    label: '技术分析',
    hint: '日线 + 前复权 + 定量结构 + 缠论',
    period: 'day',
    adjust: 'qfq',
    overlays: { structure: true, chan: true, chips: false, signals: false, alerts: false, trades: false, timeline: false },
  },
  {
    id: 'swing',
    label: '短线',
    hint: '30 分钟 + 不复权 + 定量结构 + 筹码',
    period: '30m',
    adjust: 'none',
    overlays: { structure: true, chan: false, chips: true, signals: false, alerts: false, trades: false, timeline: false },
  },
  {
    id: 'review',
    label: '复盘',
    hint: '周线 + 后复权 + 缠论(看大级别结构)',
    period: 'week',
    adjust: 'hfq',
    overlays: { structure: false, chan: true, chips: false, signals: false, alerts: false, trades: false, timeline: false },
  },
]

export function workspacePreset(id: string): WorkspacePreset | null {
  return WORKSPACE_PRESETS.find(w => w.id === id) ?? null
}

/**
 * 应用模板。只设「理想档位」 —— 当前渲染器不支持时(如 ECharts 无 1m)由调用方
 * 用 fallbackPeriod 兜底, 避免 chartSession 反向依赖渲染器契约。
 */
export function applyWorkspace(id: string): void {
  const p = workspacePreset(id)
  if (!p) return
  chartSession.patch({ period: p.period, adjust: p.adjust, overlays: { ...p.overlays } })
}

/** React 侧订阅(只订阅影响 UI 的字段, 不含 viewport) */
export function useChartSession(): ChartSessionState {
  return useSyncExternalStore(chartSession.subscribe, chartSession.getState, chartSession.getState)
}

/** 非 React 场景(图表回调里)直接读快照 */
export function readChartSession(): ChartSessionState {
  return chartSession.getState()
}
