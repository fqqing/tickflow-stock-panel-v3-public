// 后端 API 客户端 — 全项目统一入口
//
// Dev: Vite 按启动脚本解析出的 BACKEND_HOST/BACKEND_PORT 代理 /api
// Prod:同源(FastAPI 托管前端 dist)

import { toast } from '@/components/Toast'
import type { AlertTriggerPoint, BacktestTradeMark } from './chart-events'
import { BUILTIN_SIGNAL_DEFINITIONS } from './signals'

const BASE = ''

type RequestOptions = RequestInit & {
  /** 为 true 时不弹错误 toast（由调用方自行汇总提示，如多图串行队列） */
  quiet?: boolean
}

async function request<T>(path: string, init?: RequestOptions): Promise<T> {
  const { quiet, ...fetchInit } = init ?? {}
  const isFormData = fetchInit.body instanceof FormData
  const headers: Record<string, string> = {}
  if (!isFormData) headers['Content-Type'] = 'application/json'
  // 合并调用方传入的 headers (此前会被整体覆盖丢弃)
  Object.assign(headers, fetchInit.headers as Record<string, string> | undefined)
  const res = await fetch(`${BASE}${path}`, { ...fetchInit, headers })
  if (!res.ok) {
    let detail = ''
    try {
      const j = JSON.parse(await res.text())
      const raw = j.detail ?? j.message ?? ''
      if (Array.isArray(raw)) {
        // FastAPI 422 校验错误: [{type, loc, msg, input}, ...] → 取 msg 拼接
        detail = raw.map((e: any) => e?.msg || String(e)).join('; ')
      } else if (typeof raw === 'string') {
        detail = raw
      } else if (raw && typeof raw === 'object') {
        detail = JSON.stringify(raw)
      }
    } catch { /* ignore */ }
    const msg = detail || `${res.status} ${res.statusText}`
    // 401 (未登录/会话过期) 不弹 toast — 由全局认证拦截器统一跳登录页, 避免刷屏
    if (res.status !== 401 && !quiet) toast(msg, 'error')
    throw new Error(msg)
  }
  return res.json() as Promise<T>
}

// ===== Capabilities =====
export interface CapabilityLimits {
  rpm: number | null
  batch: number | null
  subscribe: number | null
}

export interface CapabilitiesResponse {
  label: string
  capabilities: Record<string, CapabilityLimits>
}

// ===== Financials =====
export interface FinancialStatus {
  available: boolean
  tables: Record<string, { rows: number; symbols: number }>
  last_sync: Record<string, string>
  /** 服务端是否正在同步(手动触发)——驱动"同步中"UI 并防重复点击 */
  syncing?: boolean
}

export interface FinancialMetricRecord {
  symbol?: string
  period_end: string
  announce_date?: string | null
  eps_basic?: number | null
  eps_diluted?: number | null
  bps?: number | null
  ocfps?: number | null
  roe?: number | null
  roe_diluted?: number | null
  roa?: number | null
  gross_margin?: number | null
  net_margin?: number | null
  debt_to_asset_ratio?: number | null
  revenue_yoy?: number | null
  net_income_yoy?: number | null
  operating_cash_to_revenue?: number | null
  inventory_turnover?: number | null
  [key: string]: any
}

export interface FinancialIncomeRecord {
  symbol?: string
  period_end: string
  announce_date?: string | null
  revenue?: number | null
  operating_cost?: number | null
  operating_profit?: number | null
  total_profit?: number | null
  net_income?: number | null
  net_income_attributable?: number | null
  basic_eps?: number | null
  diluted_eps?: number | null
  [key: string]: any
}

export interface FinancialBalanceSheetRecord {
  symbol?: string
  period_end: string
  announce_date?: string | null
  total_assets?: number | null
  total_current_assets?: number | null
  cash_and_equivalents?: number | null
  total_liabilities?: number | null
  total_equity?: number | null
  equity_attributable?: number | null
  [key: string]: any
}

export interface FinancialCashFlowRecord {
  symbol?: string
  period_end: string
  announce_date?: string | null
  net_operating_cash_flow?: number | null
  net_investing_cash_flow?: number | null
  net_financing_cash_flow?: number | null
  capex?: number | null
  net_cash_change?: number | null
  [key: string]: any
}

export interface FinancialSharesRecord {
  symbol?: string
  period_end: string
  announce_date?: string | null
  total_shares?: number | null
  float_shares?: number | null
  [key: string]: any
}

/** AI 财务分析历史报告 */
export interface AiFinancialReport {
  id: string
  symbol: string
  name: string
  focus: string
  content: string
  periods?: number
  summary?: string
  created_at: string
}

// ===== 个股分析 =====
export type LevelType = 'sr' | 'pivot' | 'extreme' | 'boll' | 'keltner_s' | 'keltner_m' | 'keltner_l' | 'atr_stop' | 'gap' | 'fib' | 'round'

export interface PriceLevel {
  value: number
  label: string
  type: LevelType
  side: 'resistance' | 'support' | 'neutral'
  strength?: 'strong' | 'medium' | 'weak'
  /** 档位(仅 pivot 有):0=P, 1=R1/S1, 2=R2/S2, 3=R3/S3。前端按"显示到第几档"过滤。 */
  rank?: number
}

/** 带状曲线指标(布林带/Keltner/ATR)的每日时间序列,与 dates 对齐。 */
export interface LevelSeries {
  boll?: { upper: (number | null)[]; lower: (number | null)[]; mid?: (number | null)[] }
  keltner_s?: { upper: (number | null)[]; lower: (number | null)[] }
  keltner_m?: { upper: (number | null)[]; lower: (number | null)[] }
  keltner_l?: { upper: (number | null)[]; lower: (number | null)[] }
  atr?: { stop_loss: (number | null)[]; take_profit: (number | null)[] }
}

export interface StockLevels {
  levels: Record<LevelType, PriceLevel[]>
  close: number | null
  summary: string
  symbol: string
  /** dates 与 series 对齐;前端按自身 rows 的日期映射,缺失填 null */
  dates?: string[]
  series?: LevelSeries
}

/** S3 筹码分布: bins 按价格从低到高, ratio 为该档筹码占比(合计 1)。 */
export interface ChipsDistribution {
  ok: boolean
  symbol: string
  bins: { price: number; ratio: number }[]
  step: number
  low: number | null
  high: number | null
  close: number | null
  avg_cost: number | null
  /** 获利盘比例(成本低于现价的筹码占比), 0~1 */
  profit_ratio: number | null
  peak_price: number | null
  concentration: { p70: [number, number] | null; p90: [number, number] | null }
  days: number
}

export interface AiStockReport {
  id: string
  symbol: string
  name: string
  focus: string
  content: string
  summary?: string
  close?: number | null
  levels?: Record<LevelType, PriceLevel[]>
  created_at: string
}

// ===== Kline =====
export interface MinuteKlineRow {
  datetime: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  amount: number
}

export interface MinuteKlineSession {
  date: string
  prev_close: number | null
  rows: MinuteKlineRow[]
}

export interface PriceLimitInfo {
  rate: number
  limit_up: number | null
  limit_down: number | null
  source: 'rule' | 'instrument'
}

/** 资讯: 新闻条目 */
export interface NewsItem {
  id: string
  title: string
  date: string
  summary: string
  url: string
  source: string
}

/** 资讯: 公告条目 (正文需按 art_code 再取) */
export interface AnnItem {
  art_code: string
  title: string
  date: string
  display_time: string
  columns: string[]
}

/**
 * K 线图绘图所需的列白名单 (传给 /api/kline/daily 的 fields 参数)。
 *
 * enriched 表一行有 70+ 列, 其中 20 个 signal_* 布尔列和 momentum_* 等 K 线图
 * 完全用不到。实测 days=1000 时全量响应 1.32MB / 2.5s, 裁剪后约 0.4MB —— 传输
 * 与 JSON 解析都是 K 线图打开慢的主要成本。
 *
 * 新增副图指标时, 记得把它依赖的列加进来, 否则图上会缺线。
 */
export const KLINE_CHART_FIELDS = [
  // 基础 OHLCV + 信息栏
  'date', 'open', 'high', 'low', 'close', 'volume', 'amount',
  'turnover_rate', 'change_pct', 'prev_close', 'amplitude',
  // 均线 / 成交量均线
  'ma5', 'ma10', 'ma20', 'ma30', 'ma60',
  'ema5', 'ema10', 'ema20', 'ema30', 'ema60', 'vol_ma5', 'vol_ma10',
  // 副图指标
  'macd_dif', 'macd_dea', 'macd_hist',
  'boll_upper', 'boll_lower',
  'kdj_k', 'kdj_d', 'kdj_j',
  'rsi_6', 'rsi_14', 'rsi_24',
  'atr_14', 'vol_ratio_5d',
  // 解密公式派生 (indicators= 注入)
  'td_signal', 'td_a3', 'cm_value',
  'st_dsg', 'st_dxg', 'st_csg', 'st_cxg', 'st_icon', 'st_dn', 'st_up',
  'ms_diff', 'ms_dea', 'ms_hist', 'ms_btext', 'ms_by', 'ms_ttext', 'ms_ty',
].join(',')

/**
 * 策略信号列(signal_*): 只有打开信号标记时才随 fields 下发。
 *
 * 不放进 KLINE_CHART_FIELDS 是因为它们是布尔列, 1000 根会多出两万多个 key
 * (未压缩约 +300KB), 而多数时候用户只看 K 线不看信号 —— 按需下发更划算。
 * 清单直接从 signals.ts 取, 避免后端加信号时两处维护漂移。
 */
export const KLINE_SIGNAL_FIELDS = BUILTIN_SIGNAL_DEFINITIONS.map((d) => d.id).join(',')

/** K 线图请求的 fields: 打开信号标记时追加信号列 */
export function klineChartFields(withSignals: boolean): string {
  return withSignals ? `${KLINE_CHART_FIELDS},${KLINE_SIGNAL_FIELDS}` : KLINE_CHART_FIELDS
}

export interface KlineRow {
  symbol?: string
  date: string
  open: number
  high: number
  low: number
  close: number
  volume?: number
  change_pct?: number
  ma5?: number | null
  ma20?: number | null
  ma60?: number | null
  macd_dif?: number | null
  macd_dea?: number | null
  macd_hist?: number | null
  rsi_14?: number | null
  vol_ratio_5d?: number | null
  /** 趋势擒龙（蛟龙出海）信号日标记，来自 indicators=trend_dragon */
  td_signal?: boolean | null
  /** 趋势擒龙的 A3（上次「9 连阳」距今 bar 数），null 表示尚未出现 */
  td_a3?: number | null
  /** 资金动能值，来自 indicators=capital_momentum；数据不足 52 根时为 null */
  cm_value?: number | null
  /** 主图定量结构：短轨道 EMA(HIGH/LOW,25)，来自 indicators=structure */
  st_dsg?: number | null
  st_dxg?: number | null
  /** 主图定量结构：长轨道 EMA(HIGH/LOW,89) */
  st_csg?: number | null
  st_cxg?: number | null
  /** 主图定量结构交叉图标：0 无 / 4 收盘上穿短上轨 / 5 收盘跌破短下轨 */
  st_icon?: number | null
  /** 下跌九转标注数字（0 表示当日无标注，否则 6~9），画在 LOW 下方 */
  st_dn?: number | null
  /** 上涨九转标注数字，画在 HIGH 上方 */
  st_up?: number | null
  /** MACD 定量结构：DIFF/DEA/柱，来自 indicators=macd_structure */
  ms_diff?: number | null
  ms_dea?: number | null
  ms_hist?: number | null
  /** 底部结构标注：1 结构形成 / 2 钝化 / 3 钝化消失；ms_by 为标注纵坐标 */
  ms_btext?: number | null
  ms_by?: number | null
  /** 顶部结构标注：1 结构形成 / 2 钝化 / 3 钝化消失；ms_ty 为标注纵坐标 */
  ms_ttext?: number | null
  ms_ty?: number | null
  [key: string]: any
}

// ===== Watchlist =====
export interface WatchlistEntry {
  symbol: string
  added_at: string
  note?: string
  name?: string | null
  /** 所属分组 id 列表 (同一标的可属于多个分组; 空数组=未分组) */
  group_ids?: string[]
}

export type WatchlistGroupColor =
  | 'sky'
  | 'blue'
  | 'indigo'
  | 'violet'
  | 'fuchsia'
  | 'rose'
  | 'orange'
  | 'amber'
  | 'lime'
  | 'emerald'
  | 'teal'
  | 'cyan'

export interface WatchlistGroup {
  id: string
  name: string
  color: WatchlistGroupColor
}

export interface WatchlistImportCandidate {
  code: string
  symbol: string | null
  name: string | null
  matched: boolean
  already_in_watchlist: boolean
}

/** 文本/表格导入的解析布局元信息（用于「已识别第 N 列是代码」这类提示）。 */
export interface WatchlistImportDetected {
  delimiter: string
  code_column: number | null
  name_column: number | null
  skipped_header: boolean
  total_rows: number
  used_rows: number
  truncated: boolean
  unmatched_inputs: string[]
}

export interface WatchlistImportResult {
  provider: string
  codes: string[]
  candidates: WatchlistImportCandidate[]
  matched_count: number
  unmatched_count: number
  /** 仅文本/表格导入返回 */
  source?: 'text' | 'file'
  detected?: WatchlistImportDetected
}

export interface Quote {
  symbol: string
  price?: number
  pct?: number
  close?: number
  change_pct?: number
  [key: string]: any
}

export interface IndexInstrument {
  symbol: string
  name?: string | null
  code?: string | null
  asset_type?: 'index'
  [key: string]: any
}

export interface IndexQuote {
  symbol: string
  name?: string | null
  last_price?: number | null
  close?: number | null
  prev_close?: number | null
  change_pct?: number | null
  change_amount?: number | null
  open?: number | null
  high?: number | null
  low?: number | null
  volume?: number | null
  amount?: number | null
  timestamp?: number | null
  [key: string]: any
}

// ===== Screener =====
export interface ScreenerStrategy {
  id: string
  name: string
  description: string
  source?: string
}

export interface StrategyLoadError {
  file: string
  error: string
}

export interface ScreenerResult {
  as_of: string
  strategy: string | null
  rows: any[]
  total: number
  elapsed_ms: number
}

export interface ScreenerResultSummary {
  total: number
  as_of: string
}

export interface ScreenerCachedSummary {
  as_of: string | null
  results: Record<string, ScreenerResultSummary>
  today_ever_counts: Record<string, number>
  updated_at: number | null
}

export interface ScreenerCachedResult {
  result: ScreenerResult | null
  today_ever_rows: Record<string, any> | null
  strategy_ids_by_symbol: Record<string, string[]>
  updated_at: number | null
}

// ===== 缠论 (笔 / 中枢 / 一买二买三买) =====

/** 缠论买卖点标注结果 (POST /api/chan/annotate 的单行)。 */
export interface ChanAnnotation {
  symbol: string
  name?: string | null
  /** 最近买点类型: 1buy/2buy/3buy; 超出 recent_bars 或无买点时为 null */
  kind: string | null
  /** 中文标签: 一买/二买/三买/无买点/数据不足 */
  label: string
  /** 买点距今多少根 K 线 */
  bars_since: number | null
  price: number | null
  trend: string | null
  text: string
  stale?: boolean
  /** 失效检查: 最新收盘已跌破买点信号价 (结构被破坏) */
  invalid?: boolean
  /** 最近卖点类型: 1sell/2sell/3sell; 与买点同一次分析结果, 不额外增加开销 */
  sell_kind?: string | null
  sell_label?: string
  sell_bars_since?: number | null
  sell_price?: number | null
  sell_trend?: string | null
  sell_text?: string
  sell_stale?: boolean
  /** 失效检查: 最新收盘已升破卖点信号价 (卖点后新高, 结构转强) */
  sell_invalid?: boolean
  /** 合成的当前状态: 取买卖两侧更近者主导, 叠加失效与买点距离提示 */
  state_side?: 'buy' | 'sell' | null
  /** 如 买后4天·贴近买点价 / 卖后14天·已新高失效 / 无信号 */
  state_label?: string | null
  state_bars_since?: number | null
  state_price?: number | null
  state_invalid?: boolean
  state_text?: string
}

export interface ChanSignalPoint {
  kind: string
  label: string
  is_buy: boolean
  index: number
  date: string | null
  price: number
  /** 是否伴随 MACD 背驰 (仅一买/一卖有意义) */
  divergence: boolean
  center_zd: number | null
  center_zg: number | null
}

export interface ChanStrokePoint {
  start_index: number
  end_index: number
  start_date: string | null
  end_date: string | null
  start_price: number
  end_price: number
  /** +1 向上笔, -1 向下笔 */
  direction: number
}

export interface ChanCenter {
  start_index: number
  end_index: number
  start_date: string | null
  end_date: string | null
  zd: number
  zg: number
  stroke_count: number
}

/** 「当下点位」快照: 买点用 snapshot, 卖点用 sell_snapshot, 结构完全对称 */
export interface ChanSnapshotPayload {
  kind: string | null
  label: string
  bars_since: number | null
  price: number | null
  center_zd: number | null
  center_zg: number | null
  trend: string
  text: string
}

export interface ChanAnalysis {
  symbol: string
  name?: string | null
  bars: number
  dates: string[]
  /** up 上涨 / down 下跌 / range 盘整 */
  trend: string
  snapshot: ChanSnapshotPayload
  sell_snapshot: ChanSnapshotPayload
  strokes: ChanStrokePoint[]
  centers: ChanCenter[]
  signals: ChanSignalPoint[]
  counts: Record<string, number>
}

export interface ChanScanItem {
  symbol: string
  name?: string | null
  kind: string
  label: string
  /** true 买点 / false 卖点 (扫描结果里买卖点混排) */
  is_buy?: boolean
  bars_since: number
  price: number | null
  trend: string
  center_zd: number | null
  center_zg: number | null
  text: string
}

export interface ChanScanResult {
  as_of: string
  kinds: string[]
  strict: boolean
  scanned: number
  hit_count: number
  elapsed_ms: number
  frame_ms?: number
  items: ChanScanItem[]
}

export interface MarketSnapshotRow {
  symbol: string
  name?: string | null
  close?: number | null
  change_pct?: number | null
  amount?: number | null
  volume?: number | null
  turnover_rate?: number | null
  vol_ratio_5d?: number | null
  total_shares?: number | null
  float_shares?: number | null
  market_cap?: number | null
  float_market_cap?: number | null
  consecutive_limit_ups?: number | null
  [key: string]: any
}

export interface OverviewDimensionRankItem {
  name: string
  count: number
  avg_pct: number
  up_count: number
  down_count: number
  amount: number
  leader?: {
    symbol?: string | null
    name?: string | null
    change_pct?: number | null
  } | null
}

export interface OverviewMarket {
  as_of: string | null
  quote_status: {
    enabled?: boolean
    running?: boolean
    quote_age_ms?: number | null
    is_trading_hours?: boolean
    [key: string]: any
  }
  indices: IndexQuote[]
  breadth: {
    total: number
    up: number
    down: number
    flat: number
    up_pct: number
    down_pct: number
    avg_pct?: number | null
    median_pct?: number | null
    strong_up?: number
    strong_down?: number
  }
  amount: { total: number; avg: number }
  boards: { board: string; count: number; up: number; down: number; up_pct: number; amount: number }[]
  limit: { limit_up: number; broken: number; failed: number; limit_down: number; max_boards: number; seal_rate?: number; tiers: { boards: number; count: number; stocks?: { symbol: string; name?: string; amount?: number }[] }[]; sealed_ready?: boolean; fake_up?: number; fake_down?: number }
  distribution: { label: string; count: number; pct: number }[]
  trend: { above_ma5: number; above_ma20: number; above_ma60: number; above_ma5_pct: number; above_ma20_pct: number; above_ma60_pct: number; new_high: number; new_low: number }
  activity: { avg_turnover: number; high_turnover: number; high_vol_ratio: number; vol_ratio: number }
  radar: { key: string; label: string; value: number }[]
  emotion: { score: number; label: string }
  top_gainers: MarketSnapshotRow[]
  top_losers: MarketSnapshotRow[]
  turnover_leaders: MarketSnapshotRow[]
  active_leaders: MarketSnapshotRow[]
  concept_rank: { leading: OverviewDimensionRankItem[]; lagging: OverviewDimensionRankItem[] }
  industry_rank: { leading: OverviewDimensionRankItem[]; lagging: OverviewDimensionRankItem[] }
}

// ===== 概念涨幅轮动矩阵 =====
// dates: 日期字符串列表(最新在最前); columns: {日期: [[概念名, 涨幅小数], ...]} 每列各自降序
export interface RpsRotationData {
  dates: string[]
  columns: Record<string, [string, number][]>
  concept_count: number
}

// ===== 市场环境(Regime) =====
export type RegimeState = 'strong' | 'lean_strong' | 'range' | 'lean_weak' | 'weak'

export const REGIME_STATE_LABELS: Record<RegimeState, string> = {
  strong: '强势',
  lean_strong: '偏强',
  range: '震荡',
  lean_weak: '偏弱',
  weak: '弱势',
}

export const REGIME_STATE_COLORS: Record<RegimeState, string> = {
  strong: '#ef4444',      // 红(强)
  lean_strong: '#f97316', // 橙
  range: '#6b7280',       // 灰
  lean_weak: '#3b82f6',   // 蓝
  weak: '#10b981',        // 绿(弱)
}

export interface RegimeRow {
  date: string
  state: RegimeState
  score: number
  limit_up: number
  limit_down: number
  broken_limit: number
  max_consecutive: number
  seal_rate: number
  up_count: number
  down_count: number
  up_ratio: number
  index_pct: number
  above_ma20_pct: number
  total_amount: number
  avg_turnover: number
  // 4 个子维度分(0-100, 重算后才有; 旧数据可能缺) — 综合分的加权来源
  avg_pct?: number
  median_pct?: number
  strong_up_pct?: number
  strong_down_pct?: number
  profit_score?: number
  speculation_score?: number
  resilience_score?: number
  trend_score?: number
  // 情绪周期阶段与梯队指标(重算后才有; 旧数据可能缺)
  phase?: MarketPhase | null
  first_board?: number | null
  ge2_count?: number | null
  ge3_count?: number | null
  ge5_count?: number | null
  ladder_completeness?: number | null
  promo_rate?: number | null
  promo_pool?: number | null
}

export interface RegimeHistory {
  rows: RegimeRow[]
  total: number
}

export interface RegimeStateItem {
  state: RegimeState
  label: string
  count: number
  pct: number
}

export interface RegimeStates {
  distribution: RegimeStateItem[]
  days: number
}

export interface RegimeCoverage {
  rows: number
  earliest_date: string | null
  latest_date: string | null
}

// ── 市场阶段(情绪周期) 与 主线 ──
export type MarketPhase = 'ice' | 'ignite' | 'rally' | 'climax' | 'ebb' | 'repair'

export const MARKET_PHASE_LABELS: Record<MarketPhase, string> = {
  ice: '冰点',
  ignite: '启动',
  rally: '主升',
  climax: '高潮',
  ebb: '退潮',
  repair: '修复',
}

export const MARKET_PHASE_COLORS: Record<MarketPhase, string> = {
  ice: '#38bdf8',     // 天蓝(冻结)
  ignite: '#f59e0b',  // 琥珀(升温)
  rally: '#ef4444',   // 红(主升)
  climax: '#d946ef',  // 品红(极端)
  ebb: '#14b8a6',     // 青(退潮)
  repair: '#94a3b8',  // 灰(修复)
}

export const MARKET_PHASE_ORDER: MarketPhase[] = ['ice', 'ignite', 'rally', 'climax', 'ebb', 'repair']

export interface MainlineMemberStat {
  member: string
  top5_days: number
  score_sum: number
  max_boards: number
  leader_symbol: string
}

export interface PhaseSegment {
  phase: MarketPhase
  label: string
  start: string
  end: string
  days: number
  avg_height: number
  avg_first_board: number
  avg_ge2: number
  avg_promo: number | null
  avg_seal_rate: number
  top_mainlines: MainlineMemberStat[]
}

export interface PhaseSegments {
  segments: PhaseSegment[]
  total: number
}

export interface MainlineRow {
  date: string
  kind: string
  member: string
  limit_up_count: number
  ge2_count: number
  max_boards: number
  boards_sum: number
  rungs_filled: number
  leader_symbol: string
  score: number
  rank: number
}

export interface MainlineLeader {
  member: string
  top1_days: number
  avg_score: number
  max_boards: number
}

export interface MainlineFilter {
  min_members: number
  max_members: number
  blacklist: string[]
  exclude_st: boolean
}

export interface MainlineResult {
  rows: MainlineRow[]
  leaders: MainlineLeader[]
  membership_note: string
  filter: MainlineFilter
}

// ===== 大盘复盘 =====
export interface AiReviewReport {
  id: string
  as_of: string
  focus?: string
  content: string
  summary?: string
  emotion_score?: number | null
  emotion_label?: string
  created_at: string
}

// ===== Strategy Engine =====
export interface StrategyParamDef {
  id: string
  label: string
  type: 'float' | 'int' | 'select' | 'bool'
  default: number | string | boolean
  min?: number
  max?: number
  step?: number
  options?: string[]
}

export interface CompositeChildInfo {
  id: string
  name: string
  source: string
  weight: number
}

export interface StrategyDetail {
  id: string
  name: string
  description: string
  tags: string[]
  source: 'builtin' | 'custom' | 'ai' | 'composite'
  execution_backend: 'polars_expr' | 'matrix_native' | 'python_history_legacy' | 'composite'
  asset_types: string[]
  timeframes: string[]
  version: string
  basic_filter: Record<string, any>
  params: StrategyParamDef[]
  params_defaults: Record<string, any>
  scoring: Record<string, number>
  scoring_directions: Record<string, ScoringDirection>
  entry_signals: string[]
  exit_signals: string[]
  minute_exit_trigger_supported_signals: string[]
  stop_loss: number | null
  take_profit: number | null
  trailing_stop: number | null
  trailing_take_profit_activate: number | null
  trailing_take_profit_drawdown: number | null
  max_hold_days: number | null
  display_limit?: number
  order_by: string
  descending: boolean
  limit: number
  // 叠加策略(composite)专属: 子策略列表与合并模式。非 composite 时为 null。
  composite_children?: CompositeChildInfo[] | null
}

export type ScoringDirection = 'high' | 'low'

export interface StrategyBuildResult {
  code: string
  meta: Record<string, any>
  valid: boolean
  error: string | null
}

export type StrategyBuildStreamEvent =
  | { type: 'meta'; strategy_id?: string; step?: number }
  | { type: 'delta'; content: string }
  | ({ type: 'result' } & StrategyBuildResult)
  | { type: 'error'; message: string }

export interface StrategyCodeSaveResult {
  ok: boolean
  strategy_id: string
  source: 'ai' | 'custom' | 'composite'
  path: string
  meta: Record<string, any>
}

// ===== Custom Signals (自定义信号) =====
export interface CustomSignalCondition {
  left: string     // 字段名
  op: string       // > >= < <= == !=
  right: string    // "field:xxx" 或数字字符串
  leftDays?: number   // 左字段取几日前 (0=当日, 默认)
  rightDays?: number  // 右字段取几日前 (仅 right 为字段时有意义)
}

export interface CustomSignal {
  id: string
  name: string
  kind: 'entry' | 'exit' | 'both'
  conditions: CustomSignalCondition[]
  enabled: boolean
}

export interface CustomSignalFieldGroup {
  key: string
  label: string
  fields: { key: string; label: string }[]
}

export interface CustomSignalOptions {
  fields: { key: string; label: string }[]
  groups?: CustomSignalFieldGroup[]
  maxDays?: number
  operators: string[]
  kinds: { key: string; label: string }[]
}

export interface CustomSignalAIGenerateResult {
  name: string
  conditions: CustomSignalCondition[]
}

// ===== Monitor (监控规则 + 触发记录) =====
export interface MonitorCondition {
  field: string
  op: string              // truth | > >= < <= == !=
  value?: number | null   // op 非 truth 时必填
}

export type StrategyNotifyEvent = 'buy_signal' | 'sell_signal' | 'pool_entry' | 'pool_exit'

export type SectorKind = 'index' | 'concept' | 'industry'

export interface SectorMonitorTarget {
  key: string
  kind: SectorKind
  name: string
  symbol?: string
  source_id?: string
  field?: string
  source_field?: string
  value?: string
  level?: number | null
  available: boolean
  member_count: number
}

export interface AbnormalWindowInfo {
  /** 实时偏离值 (小数) */
  value: number
  /** 该窗口阈值 (小数) — 后端已按偏离方向取对应侧 (严重异动负向更严) */
  threshold: number
  /** 接近度 |value|/threshold */
  closeness: number
}

export type AbnormalStatus = 'triggered' | 'edge' | 'watch'

export interface AbnormalRow {
  symbol: string
  name: string | null
  board: string
  st: boolean
  close: number | null
  rt_pct: number | null
  windows: Record<string, AbnormalWindowInfo>
  max_closeness: number
  status: AbnormalStatus
}

export interface AbnormalOverview {
  asof: number
  cache_date: string | null
  bench_rt_pct: number
  includes_today: boolean
  rules: Array<{
    board: string
    st: boolean
    /** 各窗口双侧阈值 {up: 正向, down: 负向} (小数) */
    thresholds: Record<string, { up: number; down: number }>
    note: string
  }>
  counts: { triggered: number; edge: number; watch: number }
  rows: AbnormalRow[]
}

export interface MonitorRule {
  id: string
  name: string
  enabled: boolean
  type: 'strategy' | 'signal' | 'price' | 'market' | 'ladder' | 'sector' | 'abnormal'
  asset_type?: 'stock' | 'etf' | 'index'
  scope: 'symbols' | 'all' | 'sector' | 'watchlist_group'
  symbols: string[]
  /** scope=watchlist_group 时绑定的自选分组 id (成员动态解析, 增删自选自动生效) */
  group_id?: string | null
  sector?: string | null
  sector_kind?: SectorKind | null
  sector_targets?: SectorMonitorTarget[]
  sector_trigger?: 'change_pct' | 'momentum'
  threshold_pct?: number
  window_minutes?: 1 | 3 | 5 | 10 | 15
  /** abnormal 专属: 关注窗口 (any=全部) */
  abnormal_window?: 'any' | '3d' | '10d' | '30d'
  strategy_id?: string | null
  direction: 'entry' | 'exit' | 'both' | 'up' | 'down'
  notify_events?: StrategyNotifyEvent[]
  score_min?: number | null
  score_max?: number | null
  conditions: MonitorCondition[]
  logic: 'and' | 'or'
  cooldown_seconds: number
  severity: 'info' | 'warn' | 'critical'
  message: string
  webhook_url?: string
  webhook_enabled?: boolean  // 兼容老规则, 已由 webhook_channels 取代
  webhook_channels?: string[]  // 命中时推送的外部渠道 (合法值 'feishu' | 'wecom')
  created_at?: string
  runtime_warning?: string
  // ladder 专属: 封单监控
  metric?: 'sealed_vol' | 'sealed_amount'  // 量(手) / 额(元)
  threshold?: number                        // 封单 <= 此值时报警
}

export interface MonitorRuleOptions {
  threshold_fields: { key: string; label: string }[]
  builtin_signals: { key: string; label: string }[]
  custom_signals: { key: string; label: string }[]
  operators: string[]
  types: { key: string; label: string }[]
  scopes: { key: string; label: string }[]
  logics: { key: string; label: string }[]
  severities: { key: string; label: string }[]
  directions: { key: string; label: string }[]
  intraday_signal_support: {
    available: boolean
    source: string | null
    max_symbols: number
    reason: string
  }
  sector_targets: Record<SectorKind, SectorMonitorTarget[]>
}

export interface AlertEvent {
  ts: number
  rule_id?: string
  rule_name?: string
  source: string
  type: string
  symbol?: string
  name?: string | null
  message: string
  price?: number | null
  change_pct?: number | null
  signals?: string[]
  severity?: string
  strategy_id?: string
  conditions?: MonitorCondition[]
  /** 本次真正命中的条件子集(OR 规则下是 conditions 的真子集)。
   *  该字段上线前落盘的历史记录没有它 → 消费方需回退到 conditions。 */
  matched_conditions?: MonitorCondition[]
  logic?: 'and' | 'or'
  sector_kind?: SectorKind
  sector_key?: string
  sector_name?: string
  sector_source_field?: string
  sector_value?: string
  sector_level?: number | null
  window_change_pct?: number | null
  coverage_ratio?: number
  valid_count?: number
  total_count?: number
  up_count?: number
  down_count?: number
  leader?: { symbol?: string; name?: string; change_pct?: number } | null
  /** 异动边缘告警 (source=abnormal) 附加字段 */
  abnormal_window?: string
  abnormal_value?: number
  abnormal_threshold?: number
  abnormal_closeness?: number
  /** ext 富化字段 (行业/概念等), 键为 "{configId}__{fieldName}" */
  [key: string]: unknown
}

/** 生成监控规则 id (时间戳 + 随机后缀), 用户无需手动填写。 */
export function genRuleId(): string {
  const ts = Date.now().toString(36)
  const rand = Math.random().toString(36).slice(2, 6)
  return `mr_${ts}_${rand}`
}

// ===== Limit Ladder =====
export interface LimitLadderStock {
  symbol: string
  name?: string | null
  close?: number | null
  change_pct?: number | null
  consecutive_limit_ups?: number | null
  consecutive_limit_downs?: number | null
  status?: 'limit_up' | 'broken' | 'failed' | 'limit_down' | 'recovery'
    | 'high' | 'momentum' | 'volume' | null
  /** 强度档位（港美股：20日动量档 1-5） */
  boards?: number | null
  /** 五档 sealed: real=真封板, fake=假涨停(已归炸板), pending=待确认, null=降级/无能力 */
  sealed_status?: 'real' | 'fake' | 'pending' | null
  /** 封单量(买一/卖一量), 仅真封板有值 */
  sealed_vol?: number | null
  /** 最终状态为涨跌停且当天开高低收四价相同 */
  is_one_word?: boolean
}

/** 港美股主要指数（多市场扩展） */
export interface MarketIndexItem {
  symbol: string
  name: string
  code: string
  market: string
  type: string
  latest?: { date: string; close: number; change_pct: number; snapshot?: boolean } | null
}

export interface MarketIndexKlineRow {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}


export interface LimitLadderTier {  boards: number
  count: number
  stocks: LimitLadderStock[]
}

export interface LimitLadderResult {
  as_of: string
  tiers: LimitLadderTier[]
  /** 双方向涨跌停计数(修正后, 不论当前 direction) */
  counts?: { up: number; down: number }
  /** 双方向涨跌停原始计数(修正前, 供弹窗对比) */
  counts_raw?: { up: number; down: number }
  /** sealed 数据是否就绪(false→前端显示降级标识) */
  sealed_ready?: boolean
  /** sealed 数据 age(秒), null=盘后定版或无数据 */
  sealed_age?: number | null
  /** sealed 修正统计: real=真封板, fake=假涨停(归炸板), pending=待确认 */
  sealed_counts?: { real: number; fake: number; pending: number }
  /** 涨停侧 sealed 明细 */
  sealed_counts_up?: { real: number; fake: number; pending: number }
  /** 跌停侧 sealed 明细 */
  sealed_counts_down?: { real: number; fake: number; pending: number }
}

// ===== Backtest =====
export interface BacktestResult {
  run_id: string
  config: any
  stats: Record<string, any>
  equity_curve: { date: string; value: number }[]
  trades: any[]
  per_symbol_stats: { symbol: string; total_return: number }[]
}

// ===== Factor Backtest =====
export interface FactorColumn {
  id: string
  label: string
  group: string
  desc: string
}

export interface GroupStat {
  group: number
  label: string
  total_return: number
  annual_return: number
  max_drawdown: number
  sharpe: number
  win_rate: number
}

export interface FactorBacktestResult {
  run_id: string
  config: Record<string, any>
  ic_mean: number | null
  ic_std: number | null
  ir: number | null
  ic_win_rate: number | null
  ic_series: { date: string; ic: number }[]
  group_stats: GroupStat[]
  group_nav: Record<string, any>[]
  long_short_stats: Record<string, any>
  long_short_nav: { date: string; value: number }[]
  elapsed_ms: number
  n_symbols: number
  n_dates: number
  error: string | null
}

export interface FactorBatchItem {
  factor_name: string
  label: string
  group: string
  ic_mean: number | null
  ir: number | null
  ic_win_rate: number | null
  long_short_return: number | null
  long_short_max_drawdown: number | null
  n_symbols: number
  n_dates: number
  elapsed_ms: number
  error: string | null
}

export interface FactorBatchResult {
  run_id: string
  config: Record<string, any>
  results: FactorBatchItem[]
  elapsed_ms: number
  n_symbols: number
  n_dates: number
  error: string | null
}

// ===== Factor / strategy mining =====
export type MiningBudgetProfile = 'exploratory' | 'balanced' | 'strict'
export type MiningRunStatus =
  | 'queued'
  | 'running'
  | 'cancelling'
  | 'succeeded'
  | 'succeeded_with_budget_exhausted'
  | 'failed'
  | 'cancelled'
  | 'interrupted'
  | 'skipped_prerequisite'

export interface MiningAvailability {
  asset_type: 'stock' | 'etf'
  budget_profile: MiningBudgetProfile
  trading_bars: number
  required_bars: number
  outer_folds: number
  required_outer_folds: number
  eligible: boolean
  available_start: string | null
  available_end: string | null
  effective_start: string | null
  effective_end: string | null
  suggested_start: string | null
}

export interface MiningRequestV1 {
  factor_names: string[]
  strategy_ids?: string[]
  symbols?: string[] | null
  asset_type?: 'stock' | 'etf'
  start?: string | null
  end?: string | null
  budget_profile?: MiningBudgetProfile
  commission_pct?: number
  stamp_tax_pct?: number
  slippage_bps?: number
  correlation_threshold?: number
  max_combination_factors?: number
  beam_width?: number
  max_finalists?: number
  force?: boolean
}

export interface MiningRunProgress {
  phase: string
  label?: string
  done?: number
  total?: number
  percent?: number
  elapsed_ms?: number
  message?: string
}

export interface MiningRun {
  run_id: string
  signature: string
  status: MiningRunStatus
  request: MiningRequestV1
  source?: 'manual' | 'scheduled'
  created_at: string
  updated_at: string
  started_at?: string | null
  finished_at?: string | null
  data_as_of?: string | null
  progress?: MiningRunProgress | null
  error?: string | null
  reused?: boolean
  summary?: MiningResultSummary | null
}

export interface MiningResultSummary {
  factor_count: number
  selected_factor_count: number
  candidate_count: number
  valid_fold_count: number
  skipped_fold_count: number
  confidence: 'low' | 'standard' | 'high'
  budget_exhausted?: boolean
  elapsed_ms?: number
  peak_rss_bytes?: number
}

export interface MiningFactorRow {
  factor_name: string
  label?: string
  direction: 1 | -1
  score: number | null
  ic_mean: number | null
  ir: number | null
  coverage: number | null
  turnover: number | null
  spread_return?: number | null
  spread_sharpe?: number | null
  selected: boolean
  excluded_reason?: string | null
}

export interface MiningRegimeRow {
  state: 'overall' | 'strong' | 'range' | 'weak' | string
  label: string
  n_dates: number
  total_return: number | null
  sharpe: number | null
  max_drawdown: number | null
}

export interface MiningFoldRow {
  fold: number
  label?: string
  train_start?: string
  train_end?: string
  test_start?: string
  test_end?: string
  selected_factors?: string[]
  total_return: number | null
  sharpe: number | null
  max_drawdown?: number | null
  n_trades?: number | null
  skipped?: boolean
  reason?: string | null
  evaluation_kind?: 'selected' | 'cross' | 'benchmark' | null
}

export interface MiningCandidateGate {
  qualified: boolean
  reasons: string[]
}

export interface MiningCandidateRow {
  signature: string
  name: string
  kind: 'factor_combination' | 'existing_strategy'
  factor_names?: string[]
  strategy_id?: string | null
  regime_state?: string | null
  score: number | null
  oos_return: number | null
  oos_sharpe: number | null
  oos_max_drawdown: number | null
  oos_positive_fold_ratio: number | null
  oos_n_trades: number | null
  confidence: 'low' | 'standard' | 'high'
  valid_folds?: number | null
  skipped_folds?: number | null
  promoted_candidate_id?: string | null
  published_strategy_id?: string | null
  gate?: MiningCandidateGate | null
  folds?: MiningFoldRow[]
}

export interface MiningTelemetry {
  elapsed_ms?: number
  peak_rss_bytes?: number
  panel_scans?: number
  matrix_bytes?: number
  cache_hits?: number
  fold_reuses?: number
  serialized_result_bytes?: number
  phase_ms?: Record<string, number>
}

export interface MiningRequestSummary {
  asset_type: string
  budget_profile: string
  start: string | null
  end: string | null
  factor_count: number
  strategy_count: number
  commission_pct: number | null
  stamp_tax_pct: number | null
  slippage_bps: number | null
  correlation_threshold: number | null
}

export interface MiningResult {
  run_id: string
  methodology_version: string
  algorithm_version: string
  data_as_of: string | null
  summary: MiningResultSummary
  request_summary?: MiningRequestSummary | null
  factors: MiningFactorRow[]
  correlation: {
    labels: string[]
    matrix: (number | null)[][]
    pair_counts?: (number | null)[][]
    threshold: number
  }
  regimes: MiningRegimeRow[]
  candidates: MiningCandidateRow[]
  folds: MiningFoldRow[]
  telemetry: MiningTelemetry
}

export interface MiningEvent {
  id: number
  type: string
  timestamp?: string
  payload?: Record<string, unknown>
  message?: string
}

export interface MiningScheduleConfig {
  mining_schedule_enabled: boolean
  mining_schedule_weekday: number
  mining_budget_profile: Exclude<MiningBudgetProfile, 'exploratory'>
}

export type ResearchCandidateKind = 'factor' | 'strategy'
export type ResearchCandidateStatus = 'pending' | 'validated' | 'rejected'

export interface ResearchCandidate {
  id: string
  kind: ResearchCandidateKind
  name: string
  source_id: string
  config: Record<string, unknown>
  metrics: Record<string, number | string | boolean | null>
  data_as_of: string | null
  status: ResearchCandidateStatus
  created_at: string
  updated_at: string
}

export interface ResearchCandidateCreate {
  kind: ResearchCandidateKind
  name: string
  source_id: string
  config: Record<string, unknown>
  metrics: Record<string, number | string | boolean | null>
  data_as_of?: string | null
  status?: ResearchCandidateStatus
}

// ===== Strategy Backtest =====
export interface StrategyBacktestTrade {
  symbol: string
  name?: string
  entry_date: string
  exit_date: string
  entry_price: number
  exit_price: number
  pnl_pct: number
  duration: number
  exit_reason: string
  shares?: number
  lots?: number
  position_pct?: number
  entry_value?: number
  exit_value?: number
  pnl_amount?: number
  entry_score?: number | null
  entry_signal_date?: string | null
  exit_signal_date?: string | null
  blocked_exit_days?: number
  entry_signal_id?: string | null
  exit_signal_id?: string | null
}

export interface StrategyBacktestResult {
  run_id: string
  config: Record<string, any>
  stats: Record<string, any>
  equity_curve: { date: string; value: number; cash?: number; positions?: number; exposure?: number }[]
  drawdown_curve: { date: string; value: number }[]
  benchmark_curve?: { date: string; value: number; close?: number; name?: string; symbol?: string }[]
  trades: StrategyBacktestTrade[]
  per_symbol_stats: {
    symbol: string
    n_trades: number
    total_return: number
    win_rate: number
    best: number
    worst: number
  }[]
  strategy_info: {
    id: string
    name: string
    description: string
    entry_signals: string[]
    exit_signals: string[]
    stop_loss: number | null
    take_profit: number | null
    trailing_stop: number | null
    trailing_take_profit_activate: number | null
    trailing_take_profit_drawdown: number | null
    score_min: number | null
    score_max: number | null
    max_hold_days: number | null
    source: string
    execution_backend?: string
    // 叠加策略回测: 子策略构成与权重归因
    composite_children?: { id: string; weight: number }[]
  }
  elapsed_ms: number
  error: string | null
}

// ===== Settings =====

/** 端点发现清单 —— 对应 tickflow.org/endpoints.json */
export interface EndpointItem {
  id: string
  url: string
  label: string
  region?: string
  description?: string
  premium?: boolean
}

export interface EndpointManifest {
  version?: number
  description?: string
  healthPath?: string
  /** 每端点测试轮数,用于 /health 多轮探测取中位数 */
  testRounds?: number
  endpoints: EndpointItem[]
  /** 数据来源:remote=远程拉取 / fallback=内置回退列表 */
  source?: 'remote' | 'fallback'
}

export interface SettingsState {
  mode: 'none' | 'free' | 'api_key'
  tickflow_api_key_masked: string
  has_tickflow_key: boolean
  tier_label: string
  current_endpoint: string
  probe_log: string[]
  missing_caps: string[]
  extras_caps: string[]
  // 首次使用引导
  onboarding_completed: boolean
  // AI 配置
  ai_provider: string
  ai_base_url: string
  ai_api_key_masked: string
  has_ai_key: boolean
  ai_configured?: boolean
  ai_model: string
  ai_openai_model?: string
  ai_reasoning_effort?: string
  ai_codex_model?: string
  ai_codex_command?: string
  ai_codex_reasoning_effort?: string
  ai_user_agent: string
  ai_max_output_tokens?: number
  ai_context_window?: number
}

/** 保存 TickFlow Key 的响应(先探后存) */
export interface SaveTickflowKeyResult {
  ok: boolean
  /** ok=false 且 key 无效时的原因标识,前端据此提示「Key 无效」 */
  reason?: 'invalid'
  error?: string
  mode?: 'none' | 'free' | 'api_key'
  tier_label?: string
  current_endpoint?: string
  tickflow_api_key_masked?: string
  capabilities_count?: number
}

export interface DataSourceItem {
  name: string
  display_name: string
  datasets: string[]
  path?: string | null
}

/** 内置可选插件数据源 (plugins/ 目录, 需手动装依赖) */
export interface PluginDataSourceItem {
  name: string
  display_name: string
  datasets: string[]
  runtime: string          // node | python | none
  available: boolean       // 依赖是否已安装
  status: string           // 可用性原因 (供 UI 显示)
  description: string
  install_hint: string     // 未装依赖时显示的安装命令
}

export interface DataSourceLoadError {
  name?: string
  path: string
  errors: string[]
}

export interface DataSourcesResponse {
  builtin: DataSourceItem[]
  plugins: PluginDataSourceItem[]
  custom: DataSourceItem[]
  errors: DataSourceLoadError[]
  config_dir: string
}

export interface DataSourceTestResult {
  provider: string
  dataset: string
  rows: number
  columns: string[]
  preview: Record<string, unknown>[]
}

export interface DatasetConfig {
  url: string
  method: string
  batch?: number | null
  rpm?: number | null
  response_path: string
  field_map: Record<string, string>
  transforms?: Record<string, string>
  symbols_param?: string
  start_param?: string
  end_param?: string
  asset_type_param?: string | null
  freq_param?: string | null
  timeout?: number | null
}

export interface AuthConfig {
  type: string
  token_env?: string | null
  header?: string
  param?: string
}

export interface CustomSourceConfig {
  name: string
  display_name: string
  auth: AuthConfig
  datasets: Record<string, DatasetConfig>
}

export interface WecomBotStatus {
  enabled: boolean
  running: boolean
  connected: boolean
  bot_id_configured: boolean
  secret_configured: boolean
  last_error: string
}

export interface Preferences {
  realtime_quotes_enabled: boolean
  indices_nav_pinned: boolean
  watchlist_groups_in_nav: boolean
  minute_sync_enabled: boolean
  minute_sync_days: number
  minute_sync_segment_days: number
  /** 分钟同步范围: all=A股全量(~5569) / focus=核心池沪深300+中证500+上证50+自选(~1000) */
  minute_sync_scope?: 'all' | 'focus'
  daily_data_provider?: string
  adj_factor_provider?: string
  minute_data_provider?: string
  realtime_data_provider?: string
  financial_data_provider?: string
  data_source_job_timeout_s: number
  data_source_long_job_timeout_s: number
  realtime_watchlist_symbols?: string[]
  realtime_pull_stock?: boolean
  realtime_pull_etf?: boolean
  realtime_pull_index?: boolean
  realtime_index_mode?: 'core' | 'all'
  realtime_index_symbols?: string[]
  pipeline_pull_a_share: boolean
  pipeline_pull_etf: boolean
  pipeline_pull_index: boolean
  pipeline_regime_enabled: boolean
  regime_batch_days: number
  regime_warmup_days: number
  pipeline_index_symbols: string
  pipeline_schedule: { hour: number; minute: number }
  instruments_schedule: { hour: number; minute: number }
  enriched_batch_size: number
  index_daily_batch_size: number
  limit_ladder_monitor_enabled: boolean
  depth_polling_interval: number
  depth_finalize_time: { hour: number; minute: number }
  review_schedule: { enabled: boolean; hour: number; minute: number }
  review_push_channels: string[]
  sse_refresh_pages: Record<string, boolean>
  strategy_monitor_enabled: boolean
  strategy_monitor_ids: string[]
  system_notify_enabled: boolean
  feishu_webhook_url?: string
  feishu_webhook_secret?: string
  wecom_webhook_url?: string
  wecom_bot_id?: string
  wecom_bot_secret?: string
  wecom_bot_enabled?: boolean
  webhook_enabled_default?: boolean
  webhook_default_channels?: string[]
  sidebar_index_symbols: string[]
  nav_order: string[]
  nav_hidden: string[]
  screener_auto_run: boolean
  minute_intraday_refresh: boolean
  minute_intraday_refresh_interval: number
  monitor_ext_fields: { concept: MonitorExtFieldItem | null; industry: MonitorExtFieldItem | null }
}

/** 监控中心 ext 字段单项配置 (行业/概念标签的来源 + 显示裁剪) */
export interface MonitorExtFieldItem {
  /** "configId.fieldName" */
  field: string
  /** 显示前N个标签, 0=不限制 */
  maxTags?: number
  /** 隐藏的位置 (0-based), 如 [0] 表示隐藏第一个 */
  hiddenIndices?: number[]
}
export interface StrategyAlertEvent {
  source: 'strategy' | 'depth'
  type: string
  strategy_id?: string
  symbol?: string
  name?: string | null
  message: string
  price?: number | null
  change_pct?: number | null
  signals?: string[]
  /** ext 富化字段 (行业/概念等), 键为 "{configId}__{fieldName}" */
  [key: string]: unknown
}

// ===== API surface =====
// ===== Signal Lab (信号实验室) =====
export interface SignalLabStrategy {
  id: string
  name: string
  execution_backend: string | null
  supported: boolean
  warmup_bars: number | null
  category: string | null
}

export interface SignalLabStrategies {
  strategies: SignalLabStrategy[]
  context_features: string[]
}

export interface SignalLabDataset {
  strategy_id: string
  start: string | null
  end: string | null
  path: string
  rows: number | null
  mtime: number | null
  size_bytes: number | null
  columns?: string[]
  horizons?: number[]
}

export interface SignalLabRunRequest {
  strategy_id: string
  start?: string
  end?: string
  lookback_days?: number
  limit?: number
  symbols?: string[]
  horizons?: number[]
  entry_delay?: number
  max_delay_days?: number
  stop_loss?: number | null
  take_profit?: number | null
  drop_warmup?: number | null
  matrix_cache_mb?: number
  with_features?: boolean
}

export interface SignalLabRunStart {
  run_id: string
  reused: boolean
  strategy_id?: string
  start?: string
  end?: string
  symbols?: number | null
}

export interface SignalLabRunResult {
  strategy_id: string
  start: string
  end: string
  rows: number
  columns: string[]
  horizons: number[]
  n_filled: number | null
}

export interface SignalLabRun {
  id: string
  status: 'pending' | 'running' | 'succeeded' | 'failed'
  stage: string
  progress: number
  log: { ts: number; stage: string; msg: string }[]
  request: Record<string, unknown>
  created_at?: number | null
  started_at?: number | null
  finished_at?: number | null
  duration_s?: number | null
  result: SignalLabRunResult | null
  error: string | null
}

export interface SignalLabOutcomes {
  dataset: SignalLabDataset
  total: number
  offset: number
  limit: number
  columns: string[]
  rows: Record<string, unknown>[]
}

export interface SignalLabSummary {
  dataset: SignalLabDataset
  horizons: number[]
  n_signals: number
  overall: Record<string, number | null>
  groups: Record<string, unknown>[]
}

export interface SignalLabAttributionRow {
  feature: string
  bucket: string
  n_buckets: number
  mean_spread: number | null
  ret_n: number
  ret_wins: number
  ret_win_rate: number | null
  ret_mean: number | null
  ret_median: number | null
  ret_std: number | null
  ret_profit_factor: number | null
}

export interface SignalLabAttribution {
  dataset: SignalLabDataset
  horizon: number
  requested_horizon: number | null
  features: string[]
  skipped_features: string[]
  rows: SignalLabAttributionRow[]
}

/** 当日候选打分: 台账学到的「档位 -> 历史表现」 */
export interface SignalLabBucketStat {
  bucket: string
  n: number
  win_rate: number | null
  mean: number | null
  profit_factor: number | null
}

export interface SignalLabBucketModel {
  label: string
  edges: number[]
  buckets: SignalLabBucketStat[]
}

export interface SignalLabScoreReason {
  feature: string
  label: string
  bucket: string
  mean: number | null
  win_rate: number | null
  n: number
}

export interface SignalLabScoreRow {
  symbol: string
  name: string | null
  close: number | null
  change_pct: number | null
  /** 各特征所在档位历史平均收益的样本量加权平均 */
  score: number
  n_features: number
  buckets: Record<string, string>
  reasons: SignalLabScoreReason[]
}

export interface SignalLabScoreToday {
  dataset: SignalLabDataset
  as_of: string
  horizon: number
  requested_horizon: number | null
  features: string[]
  model: Record<string, SignalLabBucketModel>
  rows: SignalLabScoreRow[]
  n_candidates: number
  n_scored: number
  notes: string[]
}

/** AI 归因给出的单条参数建议 — 网格已按策略参数声明收口, 可直接送进网格搜索。 */
export interface SignalLabParamSuggestion {
  param_id: string
  label: string
  type: 'float' | 'int' | 'bool' | 'select' | string
  direction: 'up' | 'down' | 'hold'
  reason: string
  /** 数值型: {min,max,step}; bool/select: 候选值数组 */
  grid: { min: number; max: number; step: number } | (string | number | boolean)[]
  levels: (number | string | boolean)[]
  n_levels: number
}

export interface SignalLabSuggestions {
  strategy_id: string
  horizon: number
  /** ai = 模型给的建议; explore = 模型给不出, 后端按参数声明铺的探索网格(前端须标注来源) */
  source?: 'ai' | 'explore' | 'none'
  items: SignalLabParamSuggestion[]
  combos: number
}

/** AI 归因流的一帧。 */
export type SignalLabInsightEvent =
  | { type: 'meta'; horizon: number; n_buckets: number; n_signals: number; summary: string }
  | { type: 'delta'; content: string }
  | { type: 'error'; message: string }
  | SignalLabSuggestions & { type: 'suggestions' }
  | { type: 'done' }

export const api = {
  health: () => request<{ status: string; version: string; mode: string }>('/health'),

  // ===== Auth (访问认证) =====
  authStatus: () =>
    request<{ configured: boolean; authenticated: boolean }>('/api/auth/status'),
  authSetup: (password: string) =>
    request<{ ok: boolean }>('/api/auth/setup', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),
  authLogin: (password: string) =>
    request<{ ok: boolean }>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),
  authLogout: () =>
    request<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),
  authChangePassword: (oldPassword: string, newPassword: string) =>
    request<{ ok: boolean }>('/api/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
    }),

  settings: () => request<SettingsState>('/api/settings'),
  saveTickflowKey: (api_key: string) =>
    request<SaveTickflowKeyResult>('/api/settings/tickflow-key', {
      method: 'POST',
      body: JSON.stringify({ api_key }),
    }),
  clearTickflowKey: () =>
    request<any>('/api/settings/tickflow-key', { method: 'DELETE' }),

  /** 标记首次使用向导完成（持久化到后端 preferences） */
  completeOnboarding: () =>
    request<{ ok: boolean; onboarding_completed: boolean }>(
      '/api/settings/onboarding/complete', { method: 'POST' },
    ),

  /** 保存 AI 配置 */
  saveAiSettings: (ai: { provider?: string; base_url?: string; api_key?: string; model?: string; reasoning_effort?: string; codex_command?: string; codex_reasoning_effort?: string; user_agent?: string; max_output_tokens?: number; context_window?: number }) =>
    request<{ ok: boolean; ai_provider?: string; ai_model?: string; ai_openai_model?: string; ai_reasoning_effort?: string; ai_codex_model?: string; ai_codex_command?: string; ai_codex_reasoning_effort?: string; ai_configured?: boolean; ai_max_output_tokens?: number; ai_context_window?: number }>('/api/settings/ai', {
      method: 'POST',
      body: JSON.stringify(ai),
    }),

  /** 一键清空 AI 配置(保留自定义 UA) */
  clearAiSettings: () =>
    request<{ ok: boolean }>('/api/settings/ai', { method: 'DELETE' }),

  preferences: () => request<Preferences>('/api/settings/preferences'),
  dataSources: () => request<DataSourcesResponse>('/api/settings/data-sources'),
  dataSource: (name: string) => request<CustomSourceConfig>(`/api/settings/data-sources/${encodeURIComponent(name)}`),
  saveDataSource: (config: CustomSourceConfig) =>
    request<DataSourcesResponse>('/api/settings/data-sources', {
      method: 'POST',
      body: JSON.stringify(config),
    }),
  deleteDataSource: (name: string) =>
    request<DataSourcesResponse>(`/api/settings/data-sources/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  reloadDataSources: () => request<DataSourcesResponse>('/api/settings/data-sources/reload', { method: 'POST' }),
  installPlugin: (name: string) => {
    // npm install 可能耗时较长, 用 6 分钟超时
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 360_000)
    return request<DataSourcesResponse & { install_ok: boolean; install_message: string }>(
      `/api/settings/plugins/${encodeURIComponent(name)}/install`,
      { method: 'POST', signal: controller.signal },
    ).finally(() => clearTimeout(timer))
  },
  uninstallPlugin: (name: string) =>
    request<DataSourcesResponse & { uninstall_ok: boolean; uninstall_message: string }>(
      `/api/settings/plugins/${encodeURIComponent(name)}/install`,
      { method: 'DELETE' },
    ),
  testDataSource: (
    provider: string,
    dataset: string,
    symbols?: string[],
    config?: CustomSourceConfig,
  ) =>
    request<DataSourceTestResult>('/api/settings/data-sources/test', {
      method: 'POST',
      body: JSON.stringify({ provider, dataset, symbols, config }),
    }),
  updateDataProviders: (cfg: Partial<Pick<Preferences, 'daily_data_provider' | 'adj_factor_provider' | 'minute_data_provider' | 'realtime_data_provider' | 'financial_data_provider'>>) =>
    request<Pick<Preferences, 'daily_data_provider' | 'adj_factor_provider' | 'minute_data_provider' | 'realtime_data_provider'>>(
      '/api/settings/preferences/data-providers',
      { method: 'PUT', body: JSON.stringify(cfg) },
    ),
  updateDataSourceJobTimeouts: (dataSourceJobTimeoutS: number, dataSourceLongJobTimeoutS: number) =>
    request<Pick<Preferences, 'data_source_job_timeout_s' | 'data_source_long_job_timeout_s'>>(
      '/api/settings/preferences/data-source-job-timeouts',
      {
        method: 'PUT',
        body: JSON.stringify({
          data_source_job_timeout_s: dataSourceJobTimeoutS,
          data_source_long_job_timeout_s: dataSourceLongJobTimeoutS,
        }),
      },
    ),
  updateMinuteSync: (enabled: boolean, days: number, segmentDays?: number, scope?: 'all' | 'focus') =>
    request<Preferences>('/api/settings/preferences/minute-sync', {
      method: 'PUT',
      body: JSON.stringify({
        minute_sync_enabled: enabled,
        minute_sync_days: days,
        ...(segmentDays != null ? { minute_sync_segment_days: segmentDays } : {}),
        ...(scope != null ? { minute_sync_scope: scope } : {}),
      }),
    }),
  updatePipelinePullTypes: (cfg: Partial<Pick<Preferences, 'pipeline_pull_a_share' | 'pipeline_pull_etf' | 'pipeline_pull_index'>>) =>
    request<{
      pipeline_pull_a_share: boolean
      pipeline_pull_etf: boolean
      pipeline_pull_index: boolean
    }>('/api/settings/preferences/pipeline-pull-types', {
      method: 'PUT',
      body: JSON.stringify(cfg),
    }),
  updatePipelineRegimeEnabled: (enabled: boolean) =>
    request<{ pipeline_regime_enabled: boolean }>('/api/settings/preferences/pipeline-regime-enabled', {
      method: 'PUT',
      body: JSON.stringify({ pipeline_regime_enabled: enabled }),
    }),
  updateRegimeBatchParams: (params: { batch_days?: number; warmup_days?: number }) =>
    request<{ regime_batch_days: number; regime_warmup_days: number }>('/api/settings/preferences/regime-batch-params', {
      method: 'PUT',
      body: JSON.stringify(params),
    }),
  updatePipelineIndexSymbols: (symbols: string) =>
    request<{ pipeline_index_symbols: string }>('/api/settings/preferences/pipeline-index-symbols', {
      method: 'PUT',
      body: JSON.stringify({ symbols }),
    }),
  updateRealtimeQuotes: (enabled: boolean) =>
    request<{ realtime_quotes_enabled: boolean; realtime_allowed?: boolean; mode?: string; error?: string }>('/api/settings/preferences/realtime-quotes', {
      method: 'PUT',
      body: JSON.stringify({ realtime_quotes_enabled: enabled }),
    }),
  updateRealtimeQuoteScope: (cfg: Partial<Pick<Preferences, 'realtime_pull_stock' | 'realtime_pull_etf' | 'realtime_pull_index' | 'realtime_index_mode' | 'realtime_index_symbols'>>) =>
    request<Partial<Preferences>>('/api/settings/preferences/realtime-quote-scope', {
      method: 'PUT',
      body: JSON.stringify(cfg),
    }),
  updateIndicesNavPinned: (pinned: boolean) =>
    request<{ indices_nav_pinned: boolean }>('/api/settings/preferences/indices-nav-pinned', {
      method: 'PUT',
      body: JSON.stringify({ indices_nav_pinned: pinned }),
    }),
  updateWatchlistGroupsInNav: (enabled: boolean) =>
    request<{ watchlist_groups_in_nav: boolean }>('/api/settings/preferences/watchlist-groups-in-nav', {
      method: 'PUT',
      body: JSON.stringify({ watchlist_groups_in_nav: enabled }),
    }),
  quoteStatus: () =>
    request<{
      enabled: boolean
      running: boolean
      paused?: boolean
      mode?: 'none' | 'watchlist' | 'full_market'
      realtime_allowed?: boolean
      interval_s: number
      symbol_count: number
      watchlist_symbol_count?: number
      index_symbol_count?: number
      etf_symbol_count?: number
      quote_age_ms: number | null
      is_trading_hours: boolean
      is_polling_window?: boolean
      market_phase?: string
      final_sync_done?: boolean
      final_sync_failed?: string | null
      last_fetch_ms: number | null
    }>('/api/intraday/status'),
  quoteInterval: () =>
    request<{ interval: number; min_interval: number; max_interval: number }>(
      '/api/settings/preferences/quote-interval',
    ),
  updateQuoteInterval: (interval: number) =>
    request<{ interval: number; min_interval: number; max_interval: number }>(
      '/api/settings/preferences/quote-interval',
      { method: 'PUT', body: JSON.stringify({ interval }) },
    ),
  intradayRefresh: () => request<{ status: string }>('/api/intraday/refresh', { method: 'POST' }),
  indexQuotes: (symbols?: string[]) =>
    request<{ rows: IndexQuote[]; count: number }>(
      `/api/intraday/indices${symbols?.length ? `?symbols=${encodeURIComponent(symbols.join(','))}` : ''}`,
    ),
  updateRealtimeMonitorConfig: (cfg: {
    sse_refresh_pages?: Record<string, boolean>
    strategy_monitor_enabled?: boolean
    strategy_monitor_ids?: string[]
    sidebar_index_symbols?: string[]
    screener_auto_run?: boolean
    minute_intraday_refresh?: boolean
    minute_intraday_refresh_interval?: number
    monitor_ext_fields?: { concept: MonitorExtFieldItem | null; industry: MonitorExtFieldItem | null }
  }) =>
    request<{
      sse_refresh_pages: Record<string, boolean>
      strategy_monitor_enabled: boolean
      strategy_monitor_ids: string[]
      sidebar_index_symbols: string[]
      screener_auto_run: boolean
      minute_intraday_refresh: boolean
      minute_intraday_refresh_interval: number
      monitor_ext_fields: { concept: MonitorExtFieldItem | null; industry: MonitorExtFieldItem | null }
    }>('/api/settings/preferences/realtime-monitor', {
      method: 'PUT',
      body: JSON.stringify(cfg),
    }),
  updateSystemNotify: (enabled: boolean) =>
    request<{ system_notify_enabled: boolean }>('/api/settings/preferences/system-notify', {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),
  updateFeishuWebhook: (url: string, secret: string = '') =>
    request<{ feishu_webhook_url: string; feishu_webhook_secret: string }>('/api/settings/preferences/feishu-webhook', {
      method: 'PUT',
      body: JSON.stringify({ url, secret }),
    }),
  updateWecomWebhook: (url: string) =>
    request<{ wecom_webhook_url: string }>('/api/settings/preferences/wecom-webhook', {
      method: 'PUT',
      body: JSON.stringify({ url }),
    }),
  updateWecomBot: (botId: string, secret: string, enabled: boolean = true) =>
    request<{
      wecom_bot_id: string
      wecom_bot_secret: string
      wecom_bot_enabled: boolean
      wecom_bot_status: WecomBotStatus
    }>('/api/settings/preferences/wecom-bot', {
      method: 'PUT',
      body: JSON.stringify({ bot_id: botId, secret, enabled }),
    }),
  toggleWecomBot: (enabled: boolean) =>
    request<{ wecom_bot_enabled: boolean; wecom_bot_status: WecomBotStatus }>('/api/settings/preferences/wecom-bot-toggle', {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),
  updateWebhookDefault: (enabled: boolean) =>
    request<{ webhook_enabled_default: boolean }>('/api/settings/preferences/webhook-enabled-default', {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),
  updateWebhookDefaultChannels: (channels: string[]) =>
    request<{ webhook_default_channels: string[] }>('/api/settings/preferences/webhook-default-channels', {
      method: 'PUT',
      body: JSON.stringify({ channels }),
    }),
  updatePipelineSchedule: (hour: number, minute: number) =>
    request<{ hour: number; minute: number }>('/api/settings/preferences/pipeline-schedule', {
      method: 'PUT',
      body: JSON.stringify({ hour, minute }),
    }),
  updateReviewSchedule: (enabled: boolean, hour: number, minute: number) =>
    request<{ enabled: boolean; hour: number; minute: number }>('/api/settings/preferences/review-schedule', {
      method: 'PUT',
      body: JSON.stringify({ enabled, hour, minute }),
    }),
  updateReviewPush: (channels: string[]) =>
    request<{ review_push_channels: string[] }>('/api/settings/preferences/review-push', {
      method: 'PUT',
      body: JSON.stringify({ channels }),
    }),
  updateDepthPollingInterval: (interval: number) =>
    request<{ depth_polling_interval: number }>('/api/settings/preferences/depth-polling-interval', {
      method: 'PUT',
      body: JSON.stringify({ interval }),
    }),
  updateLimitLadderMonitor: (enabled: boolean) =>
    request<{ limit_ladder_monitor_enabled: boolean }>('/api/settings/preferences/limit-ladder-monitor', {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),
  runLimitLadderFix: () =>
    request<{ ok: boolean; count: number; msg: string }>('/api/settings/preferences/limit-ladder-monitor/run', {
      method: 'POST',
    }),
  updateDepthFinalizeTime: (hour: number, minute: number) =>
    request<{ hour: number; minute: number }>('/api/settings/preferences/depth-finalize-time', {
      method: 'PUT',
      body: JSON.stringify({ hour, minute }),
    }),
  saveNavOrder: (nav_order: string[]) =>
    request<{ nav_order: string[] }>('/api/settings/preferences/nav-order', {
      method: 'PUT',
      body: JSON.stringify({ nav_order }),
    }),
  saveNavHidden: (nav_hidden: string[]) =>
    request<{ nav_hidden: string[] }>('/api/settings/preferences/nav-hidden', {
      method: 'PUT',
      body: JSON.stringify({ nav_hidden }),
    }),
  updateInstrumentsSchedule: (hour: number, minute: number) =>
    request<{ hour: number; minute: number }>('/api/settings/preferences/instruments-schedule', {
      method: 'PUT',
      body: JSON.stringify({ hour, minute }),
    }),
  updateEnrichedBatchSize: (size: number) =>
    request<{ enriched_batch_size: number }>('/api/settings/preferences/enriched-batch-size', {
      method: 'PUT',
      body: JSON.stringify({ size }),
    }),
  updateIndexDailyBatchSize: (size: number) =>
    request<{ index_daily_batch_size: number }>('/api/settings/preferences/index-daily-batch-size', {
      method: 'PUT',
      body: JSON.stringify({ size }),
    }),

  // 自选列表列配置
  watchlistColumns: () =>
    request<{ columns: any[] | null }>('/api/settings/preferences/watchlist-columns'),
  updateWatchlistColumns: (columns: any[]) =>
    request<{ columns: any[] }>('/api/settings/preferences/watchlist-columns', {
      method: 'PUT',
      body: JSON.stringify({ columns }),
    }),

  // 策略结果列表列配置
  screenerResultColumns: () =>
    request<{ columns: any[] | null }>('/api/settings/preferences/screener-result-columns'),
  updateScreenerResultColumns: (columns: any[]) =>
    request<{ columns: any[] }>('/api/settings/preferences/screener-result-columns', {
      method: 'PUT',
      body: JSON.stringify({ columns }),
    }),

  capabilities: () => request<CapabilitiesResponse>('/api/capabilities'),
  version: () => request<{ version: string }>('/api/data/version'),
  redetectCapabilities: () =>
    request<CapabilitiesResponse>('/api/capabilities/redetect', { method: 'POST' }),

  klineDaily: (
    symbol: string,
    days = 120,
    dateRange?: { start: string; end: string },
    extColumns?: string,
    indicators?: string,
    fields?: string,
    period?: string,
    adjust?: string,
  ) =>
    request<{
      symbol: string
      name?: string
      stock_info?: { name?: string; total_shares?: number; float_shares?: number; ext?: Record<string, unknown> }
      rows: KlineRow[]
      source?: string
    }>(
      (dateRange
        ? `/api/kline/daily?symbol=${encodeURIComponent(symbol)}&start_date=${dateRange.start}&end_date=${dateRange.end}`
        : `/api/kline/daily?symbol=${encodeURIComponent(symbol)}&days=${days}`)
      + (extColumns ? `&ext_columns=${encodeURIComponent(extColumns)}` : '')
      + (indicators ? `&indicators=${encodeURIComponent(indicators)}` : '')
      + (fields ? `&fields=${encodeURIComponent(fields)}` : '')
      + (period && period !== 'day' ? `&period=${encodeURIComponent(period)}` : '')
      + (adjust && adjust !== 'qfq' ? `&adjust=${encodeURIComponent(adjust)}` : ''),
    ),
  /**
   * 多分钟周期 K 线 (1/5/15/30/60/90/120 分钟)。
   * 后端两级数据源: preagg(预聚合目录, 可回溯到 2025-01) / local(1m 现场聚合),
   * 响应里的 source 标明实际走了哪条。
   * limit>0 时只返回最近 N 根(指标仍在完整数据上算好之后再截)。
   */
  klineMinuteK: (symbol: string, period = '30m', days = 120, fields?: string, limit = 0) =>
    request<{
      symbol: string
      name?: string
      period?: string
      stock_info?: { name?: string; total_shares?: number; float_shares?: number; ext?: Record<string, unknown> }
      rows: KlineRow[]
      source?: string
    }>(
      `/api/kline/minute-k?symbol=${encodeURIComponent(symbol)}&period=${encodeURIComponent(period)}&days=${days}`
      + (fields ? `&fields=${encodeURIComponent(fields)}` : '')
      + (limit > 0 ? `&limit=${limit}` : ''),
    ),
  klineDailyBatch: (symbols: string[], days = 12) =>
    request<{ data: Record<string, KlineRow[]> }>('/api/kline/daily-batch', {
      method: 'POST',
      body: JSON.stringify({ symbols, days }),
    }),

  /** 资讯: 个股新闻流 (东财公开接口, 后端带 5 分钟缓存) */
  newsStock: (symbol: string, name?: string, size = 20) =>
    request<{ symbol: string; keyword: string; items: NewsItem[] }>(
      `/api/news/stock?symbol=${encodeURIComponent(symbol)}&size=${size}`
      + (name ? `&name=${encodeURIComponent(name)}` : ''),
    ),
  /** 资讯: 个股公告列表 (只有元信息, 正文用 newsAnnContent) */
  newsAnn: (symbol: string, size = 20) =>
    request<{ symbol: string; items: AnnItem[] }>(
      `/api/news/ann?symbol=${encodeURIComponent(symbol)}&size=${size}`,
    ),
  /** 资讯: 公告正文 */
  newsAnnContent: (artCode: string) =>
    request<{ art_code: string; content: string }>(
      `/api/news/ann-content?art_code=${encodeURIComponent(artCode)}`,
    ),

  /** 五档盘口 */
  depthGet: (symbol: string) =>
    request<{
      symbol: string
      name: string
      last_price: number | null
      prev_close: number | null
      open: number | null
      high: number | null
      low: number | null
      volume: number | null
      amount: number | null
      change_pct: number | null
      turnover_rate: number | null
      pe: number | null
      pb: number | null
      total_market_cap: number | null
      circulating_market_cap: number | null
      limit_up: number | null
      limit_down: number | null
      volume_ratio: number | null
      avg_price: number | null
      bid: { price: number | null; volume: number | null }[]
      ask: { price: number | null; volume: number | null }[]
      wb_ratio: number | null
      wb_diff: number | null
      time: string | null
      timestamp: number | null
    // 注意: 这里用 quiet —— 盘口是高频轮询查询, 失败的 UI 降级已由顶栏/盘口面板
    // 自己表达("盘口不可用 · 以图表为准")。若不静默, 无盘口的标的(港美股/写错的
    // 代码)每轮轮询都会弹一条红色 toast, 实测 12s 内刷了 3 条。
    }>(`/api/depth/${encodeURIComponent(symbol)}`, { quiet: true }),

  // 缠论: 单票结构 / 批量标注 / 全市场扫描
  chanAnalysis: (symbol: string, lookback = 400, strict = true) =>
    request<ChanAnalysis>(
      `/api/chan/analysis?symbol=${encodeURIComponent(symbol)}&lookback=${lookback}&strict=${strict}`,
    ),
  chanAnnotate: (symbols: string[], lookback = 400, strict = true, recentBars = 60) =>
    request<{ items: ChanAnnotation[] }>('/api/chan/annotate', {
      method: 'POST',
      body: JSON.stringify({ symbols, lookback, strict, recent_bars: recentBars }),
    }),
  chanScan: (kinds: string, lookback = 320, recentBars = 15, strict = true, limit = 300) =>
    request<ChanScanResult>(
      `/api/chan/scan?kinds=${encodeURIComponent(kinds)}&lookback=${lookback}`
      + `&recent_bars=${recentBars}&strict=${strict}&limit=${limit}`,
    ),
  klineMinuteBatch: (symbols: string[], date?: string) =>
    request<{ data: Record<string, MinuteKlineRow[]> }>('/api/kline/minute-batch', {
      method: 'POST',
      body: JSON.stringify({ symbols, date }),
    }),
  instrumentSearch: (q: string, limit = 20, assetTypes?: string) =>
    request<{ results: { symbol: string; name: string; code: string; asset_type?: string }[] }>(
      `/api/kline/instruments/search?q=${encodeURIComponent(q)}&limit=${limit}${assetTypes ? `&asset_types=${encodeURIComponent(assetTypes)}` : ''}`,
    ),

  /** 批量查股票名称 (传入 symbol 列表, 返回 {symbol: name}) */
  instrumentNames: (symbols: string[]) =>
    request<{ names: Record<string, string> }>('/api/kline/instruments/names', {
      method: 'POST',
      body: JSON.stringify(symbols),
    }),
  klineMinute: (symbol: string, date?: string) =>
    request<{
      symbol: string
      name?: string
      stock_info?: { name?: string; total_shares?: number; float_shares?: number }
      date: string | null
      rows: MinuteKlineRow[]
      source?: 'local' | 'live' | 'none'
      asset_type?: 'stock' | 'etf' | 'index'
      price_limit?: PriceLimitInfo | null
      prev_close?: number | null
      /** 昨收来源: local=本地日K(前复权) / realtime=实时快照兜底 / none=都拿不到 */
      prev_close_source?: 'local' | 'realtime' | 'none'
    }>(
      `/api/kline/minute?symbol=${encodeURIComponent(symbol)}${date ? `&date=${date}` : ''}`,
    ),
  klineMinuteRange: (symbol: string, days = 10) =>
    request<{
      symbol: string
      name?: string
      asset_type: 'stock' | 'etf' | 'index'
      requested_days: number
      sessions: MinuteKlineSession[]
      source: 'local' | 'none'
    }>(
      `/api/kline/minute-range?symbol=${encodeURIComponent(symbol)}&days=${days}`,
    ),
  indexList: () => request<{ results: IndexInstrument[]; count: number }>('/api/index/list'),
  indexSearch: (q: string, limit = 20) =>
    request<{ results: IndexInstrument[] }>(
      `/api/index/search?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),
  indexDaily: (symbol: string, days = 120, dateRange?: { start: string; end: string }) =>
    request<{
      symbol: string
      name?: string
      index_info?: IndexInstrument
      rows: KlineRow[]
      source?: string
    }>(
      dateRange
        ? `/api/index/daily?symbol=${encodeURIComponent(symbol)}&start_date=${dateRange.start}&end_date=${dateRange.end}`
        : `/api/index/daily?symbol=${encodeURIComponent(symbol)}&days=${days}`,
    ),
  indexMinute: (symbol: string, date?: string) =>
    request<{
      symbol: string
      name?: string
      index_info?: IndexInstrument
      date: string | null
      rows: MinuteKlineRow[]
      source?: string
    }>(
      `/api/index/minute?symbol=${encodeURIComponent(symbol)}${date ? `&date=${date}` : ''}`,
    ),
  syncIndexInstruments: () =>
    request<{ status: string; count: number }>('/api/index/sync_instruments', { method: 'POST' }),
  syncIndexDaily: (days = 365) =>
    request<{ status: string; index_count: number; rows_written: number }>(
      `/api/index/sync_daily?days=${days}`,
      { method: 'POST' },
    ),
  syncSymbol: (symbol: string, days = 250) =>
    request<{ symbol: string; rows_written: number }>(
      `/api/kline/sync?symbol=${encodeURIComponent(symbol)}&days=${days}`,
      { method: 'POST' },
    ),
  syncMinute: (days?: number, extend?: boolean) =>
    request<{ status: string; job_id: string }>('/api/kline/sync_minute', {
      method: 'POST',
      body: JSON.stringify({ ...(days ? { days } : {}), ...(extend ? { extend: true } : {}) }),
    }),
  syncMinuteSingle: (symbol: string, days?: number) =>
    request<{ status: string; symbol: string; rows: number }>('/api/kline/sync_minute_single', {
      method: 'POST',
      body: JSON.stringify({ symbol, ...(days != null ? { days } : {}) }),
    }),
  clearMinute: () =>
    request<{ status: string; removed: number }>('/api/kline/clear_minute', {
      method: 'POST',
      body: JSON.stringify({ confirm: true }),
    }),
  extendHistory: (value: number, unit: 'day' | 'month' | 'year') =>
    request<{ status: string; job_id: string }>('/api/kline/extend_history', {
      method: 'POST',
      body: JSON.stringify({ value, unit }),
    }),
  repairDaily: (startDate: string) =>
    request<{ status: string; job_id: string }>('/api/kline/repair_daily', {
      method: 'POST',
      body: JSON.stringify({ start_date: startDate }),
    }),
  rebuildEnriched: () =>
    request<{ status: string; job_id: string }>('/api/kline/rebuild_enriched', {
      method: 'POST',
    }),

  watchlistList: () => request<{ symbols: WatchlistEntry[] }>('/api/watchlist'),
  watchlistAdd: (symbol: string, note = '', groupId?: string | null) =>
    request<{ symbols: WatchlistEntry[] }>('/api/watchlist', {
      method: 'POST',
      body: JSON.stringify({ symbol, note, group_id: groupId ?? null }),
    }),
  watchlistBatchAdd: (symbols: string[], note = '', groupId?: string | null) =>
    request<{ symbols: WatchlistEntry[]; added: number }>('/api/watchlist/batch', {
      method: 'POST',
      body: JSON.stringify({ symbols, note, group_id: groupId ?? null }),
    }),
  watchlistGroups: () =>
    request<{ groups: WatchlistGroup[] }>('/api/watchlist/groups'),
  watchlistGroupCreate: (name: string, color: WatchlistGroupColor) =>
    request<{ groups: WatchlistGroup[]; group: WatchlistGroup }>('/api/watchlist/groups', {
      method: 'POST',
      body: JSON.stringify({ name, color }),
    }),
  watchlistGroupRename: (groupId: string, name: string, color: WatchlistGroupColor) =>
    request<{ groups: WatchlistGroup[] }>(
      `/api/watchlist/groups/${encodeURIComponent(groupId)}`,
      { method: 'PUT', body: JSON.stringify({ name, color }) },
    ),
  watchlistGroupReorder: (orderedIds: string[]) =>
    request<{ groups: WatchlistGroup[] }>('/api/watchlist/groups/reorder', {
      method: 'PUT',
      body: JSON.stringify({ ordered_ids: orderedIds }),
    }),
  watchlistGroupDelete: (groupId: string) =>
    request<{ groups: WatchlistGroup[]; symbols: WatchlistEntry[] }>(
      `/api/watchlist/groups/${encodeURIComponent(groupId)}`,
      { method: 'DELETE' },
    ),
  watchlistGroupClear: (groupId: string) =>
    request<{ symbols: WatchlistEntry[] }>(
      `/api/watchlist/groups/${encodeURIComponent(groupId)}/clear`,
      { method: 'POST' },
    ),
  watchlistSetGroup: (symbol: string, groupId: string | null) =>
    request<{ symbols: WatchlistEntry[] }>(
      `/api/watchlist/${encodeURIComponent(symbol)}/group`,
      { method: 'PUT', body: JSON.stringify({ group_id: groupId }) },
    ),
  watchlistGroupAddMember: (groupId: string, symbol: string) =>
    request<{ symbols: WatchlistEntry[] }>(
      `/api/watchlist/groups/${encodeURIComponent(groupId)}/members/${encodeURIComponent(symbol)}`,
      { method: 'POST' },
    ),
  watchlistGroupRemoveMember: (groupId: string, symbol: string) =>
    request<{ symbols: WatchlistEntry[] }>(
      `/api/watchlist/groups/${encodeURIComponent(groupId)}/members/${encodeURIComponent(symbol)}`,
      { method: 'DELETE' },
    ),
  watchlistOcrStatus: () =>
    request<{ provider: string; available: boolean }>('/api/watchlist/ocr-status'),
  watchlistImportImage: (file: File, signal?: AbortSignal, quiet = false) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<WatchlistImportResult>('/api/watchlist/import-image', {
      method: 'POST',
      body: fd,
      signal,
      quiet,
    })
  },
  /**
   * 从粘贴文本（Excel 复制即 TSV）或 txt/csv/xlsx 文件导入自选。
   * 与 watchlistImportImage 返回同构的候选列表，前端共用确认列表。
   */
  watchlistImportSource: (
    source: { text?: string; file?: File },
    signal?: AbortSignal,
    quiet = false,
  ) => {
    const fd = new FormData()
    if (source.text !== undefined) fd.append('text', source.text)
    if (source.file) fd.append('file', source.file)
    return request<WatchlistImportResult>('/api/watchlist/import-source', {
      method: 'POST',
      body: fd,
      signal,
      quiet,
    })
  },
  watchlistRemove: (symbol: string) =>
    request<{ symbols: WatchlistEntry[] }>(
      `/api/watchlist/${encodeURIComponent(symbol)}`,
      { method: 'DELETE' },
    ),
  watchlistMoveToTop: (symbol: string) =>
    request<{ symbols: WatchlistEntry[] }>(
      `/api/watchlist/${encodeURIComponent(symbol)}/top`,
      { method: 'POST' },
    ),
  watchlistClear: () =>
    request<{ removed: number }>('/api/watchlist', { method: 'DELETE' }),
  watchlistQuotes: () => request<{ quotes: Quote[] }>('/api/watchlist/quotes'),
  watchlistEnriched: (extColumns?: string) =>
    request<{ rows: any[]; as_of: string | null; elapsed_ms: number }>(
      extColumns
        ? `/api/watchlist/enriched?ext_columns=${encodeURIComponent(extColumns)}`
        : '/api/watchlist/enriched',
    ),

  screenerStrategies: async (assetType?: 'stock' | 'etf' | 'index', market?: string) => {
    const data = await request<{ strategies: StrategyDetail[]; load_errors?: StrategyLoadError[] }>(
      `/api/strategies?${assetType ? `asset_type=${assetType}&` : ''}timeframe=1d&market=${market ?? 'cn'}`,
    )
    return { presets: data.strategies, load_errors: data.load_errors }
  },
  screenerRunPreset: (strategy_id: string, pool?: string[], asOf?: string, extColumns?: string, assetType: 'stock' | 'etf' = 'stock', market: string = 'cn') =>
    request<ScreenerResult>('/api/screener/run_preset', {
      method: 'POST',
      body: JSON.stringify({ strategy_id, pool, as_of: asOf ?? null, ext_columns: extColumns || null, asset_type: assetType, market }),
    }),
  screenerRunCustom: (conditions: string[], orderBy?: string, limit = 30, pool?: string[], extColumns?: string, assetType: 'stock' | 'etf' = 'stock', market: string = 'cn') =>
    request<ScreenerResult>('/api/screener/run', {
      method: 'POST',
      body: JSON.stringify({ conditions, order_by: orderBy, limit, pool, ext_columns: extColumns || null, asset_type: assetType, market }),
    }),
  screenerRunAll: (asOf?: string, strategyIds?: string[], assetType: 'stock' | 'etf' = 'stock', market: string = 'cn') =>
    request<{ as_of: string | null; results: Record<string, ScreenerResultSummary> }>(
      '/api/screener/run_all', { method: 'POST', body: JSON.stringify({ as_of: asOf ?? null, strategy_ids: strategyIds ?? null, asset_type: assetType, timeframe: '1d', summary_only: true, market }) },
    ),
  screenerCachedSummary: (market: string = 'cn', date?: string) =>
    request<ScreenerCachedSummary>(
      `/api/screener/cached-summary?market=${market}${date ? `&date=${encodeURIComponent(date)}` : ''}`,
    ),

  // 港美股主要指数（多市场扩展）
  indicesMarketList: (market: 'hk' | 'us') =>
    request<{ results: IndexInstrument[]; count: number }>(`/api/indices/market/list?market=${market}`),
  indicesMarket: (market: 'hk' | 'us') =>
    request<{ market: string; items: MarketIndexItem[] }>(`/api/indices/market?market=${market}`),
  indicesMarketKline: (market: 'hk' | 'us', symbol: string, days = 250) =>
    request<{ symbol: string; market: string; rows: MarketIndexKlineRow[] }>(
      `/api/indices/market/kline?market=${market}&symbol=${symbol}&days=${days}`,
    ),



  indicesMarketQuotes: (market: 'hk' | 'us') =>
    request<{ rows: IndexQuote[]; count: number }>(`/api/indices/market/quotes?market=${market}`),
  indicesMarketDaily: (market: 'hk' | 'us', symbol: string, days = 180) =>
    request<{ symbol: string; rows: KlineRow[]; index_info: IndexInstrument | null }>(
      `/api/indices/market/daily?market=${market}&symbol=${symbol}&days=${days}`,
    ),
  indicesMarketMinute: (market: 'hk' | 'us', symbol: string) =>
    request<{ symbol: string; rows: MinuteKlineRow[]; index_info: IndexInstrument | null }>(
      `/api/indices/market/minute?market=${market}&symbol=${symbol}`,
    ),
  indicesMarketSync: (market: 'hk' | 'us') =>
    request<{ ok: boolean; rows: number; market: string }>(`/api/indices/market/sync?market=${market}`, { method: 'POST' }),

  // 港美股数据版复盘（多市场扩展，不依赖 AI）
  marketRecapData: (market: 'hk' | 'us') =>
    request<{ as_of: string | null; content: string; market: string }>(
      `/api/market-recap/market-data?market=${market}`,
    ),

  // date 传入选中日期: 后端按日期槽取缓存, 切回已算过的日期直接命中, 不触发重跑。
  screenerCachedResult: (strategyId: string, extColumns?: string, market: string = 'cn', date?: string) =>
    request<ScreenerCachedResult>(
      `/api/screener/cached-result/${encodeURIComponent(strategyId)}?market=${market}`
      + (extColumns ? `&ext_columns=${encodeURIComponent(extColumns)}` : '')
      + (date ? `&date=${encodeURIComponent(date)}` : ''),
    ),
  screenerCached: (extColumns?: string, market: string = 'cn', date?: string) =>
    request<{ as_of: string | null; results: Record<string, { total: number; as_of: string; rows: any[] }>; today_ever_matched: Record<string, string[]> | null; today_ever_rows: Record<string, Record<string, any>> | null; updated_at: number | null }>(
      `/api/screener/cached?market=${market}`
      + (extColumns ? `&ext_columns=${encodeURIComponent(extColumns)}` : '')
      + (date ? `&date=${encodeURIComponent(date)}` : ''),
    ),
  marketSnapshot: () =>
    request<{ as_of: string | null; rows: MarketSnapshotRow[] }>('/api/screener/market-snapshot'),
  overviewMarket: (asOf?: string, market?: string) => request<OverviewMarket>(`/api/overview/market${asOf || market ? `?${asOf ? `as_of=${asOf}&` : ''}market=${market ?? 'cn'}` : ''}`),

  // 概念涨幅轮动矩阵: 每列(日期)各自把所有概念按当天涨幅从高到低排序
  rpsRotation: (days: number, kind?: 'concept' | 'industry', level?: number) =>
    request<RpsRotationData>(`/api/rps/rotation?days=${days}${kind ? `&kind=${kind}` : ''}${level ? `&level=${level}` : ''}`),

  // 市场环境(Regime)
  regimeHistory: (start?: string, end?: string, limit?: number, market?: string) => {
    const params = new URLSearchParams()
    if (start) params.set('start', start)
    if (end) params.set('end', end)
    if (limit) params.set('limit', String(limit))
    if (market && market !== 'cn') params.set('market', market)
    const qs = params.toString()
    return request<RegimeHistory>(`/api/regime/history${qs ? `?${qs}` : ''}`)
  },
  regimeLatest: (market?: string) => request<{ row: RegimeRow | null }>(`/api/regime/latest${market && market !== 'cn' ? `?market=${market}` : ''}`),
  regimeStates: (days = 60, market?: string) => request<RegimeStates>(`/api/regime/states?days=${days}${market && market !== 'cn' ? `&market=${market}` : ''}`),
  regimeCoverage: (market?: string) => request<RegimeCoverage>(`/api/regime/coverage${market && market !== 'cn' ? `?market=${market}` : ''}`),
  regimeRecompute: (start?: string, end?: string, market?: string) => {
    const params = new URLSearchParams()
    if (start) params.set('start', start)
    if (end) params.set('end', end)
    if (market && market !== 'cn') params.set('market', market)
    const qs = params.toString()
    return request<{ ok: boolean; computed: number; phase_days?: number; mainline_rows?: number }>(`/api/regime/recompute${qs ? `?${qs}` : ''}`, { method: 'POST' })
  },
  regimePhases: (start?: string, end?: string) => {
    const params = new URLSearchParams()
    if (start) params.set('start', start)
    if (end) params.set('end', end)
    const qs = params.toString()
    return request<PhaseSegments>(`/api/regime/phases${qs ? `?${qs}` : ''}`)
  },
  regimeMainline: (start?: string, end?: string, top = 10, kind: 'concept' | 'industry' = 'concept') => {
    const params = new URLSearchParams({ top: String(top), kind })
    if (start) params.set('start', start)
    if (end) params.set('end', end)
    return request<MainlineResult>(`/api/regime/mainline?${params.toString()}`)
  },
  regimeMainlineRecompute: () =>
    request<{ ok: boolean; rows: number }>('/api/regime/mainline/recompute', { method: 'POST' }),
  mainlineFilterUpdate: (payload: { min_members?: number; max_members?: number; blacklist?: string[]; exclude_st?: boolean }) =>
    request<MainlineFilter>('/api/settings/preferences/mainline-filter', {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),

  limitLadder: (asOf?: string, extColumns?: string, direction?: 'up' | 'down', market?: string) => {
    const params = new URLSearchParams()
    if (asOf) params.set('as_of', asOf)
    if (extColumns) params.set('ext_columns', extColumns)
    if (direction === 'down') params.set('direction', 'down')
    if (market && market !== 'cn') params.set('market', market)
    const qs = params.toString()
    return request<LimitLadderResult>(
      `/api/screener/limit-ladder${qs ? `?${qs}` : ''}`,
    )
  },

  backtestStatus: () => request<{ available: boolean }>('/api/backtest/status'),

  backtestRun: (payload: {
    symbols: string[]
    entries: string[]
    exits: string[]
    start?: string
    end?: string
    stop_loss_pct?: number
    max_hold_days?: number
    matching?: 'close_t' | 'open_t+1'
    asset_type?: 'stock' | 'etf' | 'index'
  }) =>
    request<BacktestResult>('/api/backtest/run', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  factorColumns: () =>
    request<{ columns: FactorColumn[] }>('/api/backtest/factor/columns'),

  factorRun: (payload: {
    factor_name: string
    symbols?: string[] | null
    start?: string | null
    end?: string | null
    n_groups?: number
    rebalance?: 'daily' | 'weekly' | 'monthly'
    weight?: 'equal' | 'factor_weight'
    fees_pct?: number
    slippage_bps?: number
    asset_type?: 'stock' | 'etf' | 'index'
  }) =>
    request<FactorBacktestResult>('/api/backtest/factor/run', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  factorBatch: (payload: {
    factor_names: string[]
    symbols?: string[] | null
    start?: string | null
    end?: string | null
    n_groups?: number
    rebalance?: 'daily' | 'weekly' | 'monthly'
    weight?: 'equal' | 'factor_weight'
    fees_pct?: number
    slippage_bps?: number
    asset_type?: 'stock' | 'etf' | 'index'
  }) =>
    request<FactorBatchResult>('/api/backtest/factor/batch', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  miningRuns: () =>
    request<{ items: MiningRun[] }>('/api/backtest/mining/runs'),

  miningAvailability: (params: {
    assetType: 'stock' | 'etf'
    budgetProfile: MiningBudgetProfile
    start?: string
    end?: string
  }) => {
    const query = new URLSearchParams({
      asset_type: params.assetType,
      budget_profile: params.budgetProfile,
    })
    if (params.start) query.set('start', params.start)
    if (params.end) query.set('end', params.end)
    return request<MiningAvailability>(`/api/backtest/mining/availability?${query}`, {
      quiet: true,
    })
  },

  miningRun: (runId: string) =>
    request<MiningRun>(`/api/backtest/mining/runs/${encodeURIComponent(runId)}`),

  miningStart: (payload: MiningRequestV1) =>
    request<MiningRun>('/api/backtest/mining/runs', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  miningResult: (runId: string) =>
    request<MiningResult>(`/api/backtest/mining/runs/${encodeURIComponent(runId)}/result`),

  miningCancel: (runId: string) =>
    request<MiningRun>(`/api/backtest/mining/runs/${encodeURIComponent(runId)}/cancel`, {
      method: 'POST',
    }),

  miningPromote: (runId: string, signature: string) =>
    request<ResearchCandidate>(
      `/api/backtest/mining/runs/${encodeURIComponent(runId)}/candidates/${encodeURIComponent(signature)}/promote`,
      { method: 'POST' },
    ),

  miningPublish: (runId: string, signature: string) =>
    request<{ ok: boolean; strategy_id: string }>(
      `/api/backtest/mining/runs/${encodeURIComponent(runId)}/candidates/${encodeURIComponent(signature)}/publish`,
      { method: 'POST' },
    ),

  miningConfig: () =>
    request<MiningScheduleConfig>('/api/backtest/mining/config'),

  updateMiningConfig: (payload: Partial<MiningScheduleConfig>) =>
    request<MiningScheduleConfig>('/api/backtest/mining/config', {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),

  researchCandidates: () =>
    request<{ items: ResearchCandidate[] }>('/api/backtest/candidates'),

  researchCandidateCreate: (payload: ResearchCandidateCreate) =>
    request<ResearchCandidate>('/api/backtest/candidates', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  researchCandidateUpdate: (
    id: string,
    payload: { name?: string; status?: ResearchCandidateStatus },
  ) =>
    request<ResearchCandidate>(`/api/backtest/candidates/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),

  researchCandidateDelete: (id: string) =>
    request<{ ok: boolean }>(`/api/backtest/candidates/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }),

  strategyBacktestRun: (payload: {
    strategy_id: string
    symbols?: string[] | null
    start?: string | null
    end?: string | null
    params?: Record<string, any> | null
    overrides?: Record<string, any> | null
    matching?: 'close_t' | 'open_t+1'
    entry_fill?: 'close_t' | 'open_t+1' | null
    exit_fill?: 'close_t' | 'open_t+1' | 'signal_next_minute' | null
    fees_pct?: number
    commission_pct?: number
    stamp_tax_pct?: number
    slippage_bps?: number
    max_positions?: number
    initial_capital?: number
    position_sizing?: 'equal' | 'score_weight'
    asset_type?: 'stock' | 'etf' | 'index'
    minute_fill?: boolean
    market?: 'cn' | 'hk' | 'us'
  }) =>
    request<StrategyBacktestResult>('/api/backtest/strategy/run', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  pipelineRunMarket: (market: 'hk' | 'us') =>
    request<{ job_id: string; reused: boolean; market: string }>(
      `/api/pipeline/run-market?market=${market}`, { method: 'POST' },
    ),

  pipelineRun: () => request<{ job_id: string; reused: boolean }>(
    '/api/pipeline/run', { method: 'POST' },
  ),
  pipelineJob: (id: string) => request<PipelineJob>(`/api/pipeline/jobs/${id}`),
  pipelineJobs: (limit = 20) =>
    request<{ active_id: string | null; jobs: PipelineJobSummary[] }>(
      `/api/pipeline/jobs?limit=${limit}`,
    ),

  dataStatus: () => request<DataStatus>('/api/data/status'),
  dataClear: () => request<{ deleted_files: number }>('/api/data/clear', { method: 'POST' }),
  refreshCache: () => request<{ ok: boolean }>('/api/data/refresh-cache', { method: 'POST' }),
  enrichedSchema: (table: string) => request<EnrichedField[]>(`/api/data/schema/${table}`),

  testEndpoint: (url: string, rounds?: number) =>
    request<{
      ok: boolean
      url: string
      rounds: number
      success: number
      median_ms: number | null
      min_ms?: number | null
      max_ms?: number | null
      /** 兼容旧字段,等于 median_ms */
      latency_ms?: number | null
      error?: string
    }>(
      '/api/settings/test_endpoint', {
        method: 'POST',
        body: JSON.stringify({ url, rounds }),
      },
    ),

  // 端点发现 —— 后端代理拉取 tickflow.org/endpoints.json(前端无法跨域直连)
  listEndpoints: () =>
    request<EndpointManifest>('/api/settings/endpoints'),

  switchEndpoint: (url: string) =>
    request<{ ok: boolean; current_endpoint: string; error?: string }>(
      '/api/settings/switch_endpoint', {
        method: 'POST',
        body: JSON.stringify({ url }),
      },
    ),

  // ===== 扩展数据 =====
  extDataList: () =>
    request<{ items: ExtDataConfig[] }>('/api/ext-data'),

  extDataRows: (id: string, opts?: { date?: string; limit?: number; columns?: string[] }) => {
    const qs = new URLSearchParams()
    if (opts?.date) qs.set('date', opts.date)
    if (opts?.limit) qs.set('limit', String(opts.limit))
    if (opts?.columns?.length) qs.set('columns', opts.columns.join(','))
    const suffix = qs.toString()
    return request<ExtDataRowsResult>(`/api/ext-data/${encodeURIComponent(id)}/rows${suffix ? `?${suffix}` : ''}`)
  },

  dimensionMembers: (id: string, opts: { field: string; value: string; date?: string; limit?: number }) => {
    const qs = new URLSearchParams({ field: opts.field, value: opts.value })
    if (opts.date) qs.set('date', opts.date)
    if (opts.limit) qs.set('limit', String(opts.limit))
    return request<DimensionMembersResult>(`/api/ext-data/${encodeURIComponent(id)}/dimension-members?${qs.toString()}`)
  },

  analysisMenus: () =>
    request<{ items: AnalysisMenu[] }>('/api/analysis-menus'),

  analysisMenu: (id: string) =>
    request<AnalysisMenu>(`/api/analysis-menus/${encodeURIComponent(id)}`),

  analysisMenuSave: (id: string, body: Omit<AnalysisMenu, 'id' | 'created_at' | 'updated_at' | 'builtin'>) =>
    request<AnalysisMenu>(`/api/analysis-menus/${encodeURIComponent(id)}`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  analysisMenuReorder: (ids: string[]) =>
    request<{ items: AnalysisMenu[] }>('/api/analysis-menus/reorder', {
      method: 'POST',
      body: JSON.stringify({ ids }),
    }),

  analysisMenuDelete: (id: string) =>
    request<{ status: string }>(`/api/analysis-menus/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  extDataCreate: (body: { id: string; label: string; mode: 'snapshot' | 'timeseries'; fields: { name: string; dtype: string; label: string }[]; description?: string; symbol_map?: Record<string, string>; code_map?: Record<string, string> }) =>
    request<ExtDataConfig>('/api/ext-data', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  extDataUpdate: (id: string, body: { label?: string; fields?: { name: string; dtype: string; label: string }[]; description?: string }) =>
    request<ExtDataConfig>(`/api/ext-data/${id}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  extDataDelete: (id: string) =>
    request<{ status: string }>(`/api/ext-data/${id}`, { method: 'DELETE' }),

  extDataUpload: (id: string, file: File, snapshotDate?: string) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<{ status: string; rows: number; date: string }>(
      `/api/ext-data/${id}/upload${snapshotDate ? `?snapshot_date=${snapshotDate}` : ''}`,
      { method: 'POST', body: fd },
    )
  },

  extDataIngest: (id: string, body: { date?: string; rows: Record<string, unknown>[] }) =>
    request<{ status: string; rows: number; date: string }>(
      `/api/ext-data/${id}/ingest`,
      { method: 'POST', body: JSON.stringify(body) },
    ),

  extDataSchemaAll: () =>
    request<{ items: { id: string; label: string; mode: string; columns: { name: string; type: string; label: string }[] }[] }>('/api/ext-data/schema-all'),

  extDataPullConfig: (id: string, body: {
    url: string; method?: string; headers?: Record<string, string>; body?: string;
    response_path?: string; field_map?: Record<string, string>;
    schedule_minutes?: number; enabled?: boolean;
    time_window_start?: string | null; time_window_end?: string | null;
  }) =>
    request<{ status: string; pull: PullConfig }>(
      `/api/ext-data/${id}/pull`,
      { method: 'PUT', body: JSON.stringify(body) },
    ),

  extDataPullTest: (id: string) =>
    request<{ status: string; total_rows: number; preview: Record<string, unknown>[]; has_symbol: boolean }>(
      `/api/ext-data/${id}/pull/test`,
      { method: 'POST' },
    ),

  extDataPullRun: (id: string) =>
    request<{ status: string; rows: number; date: string }>(
      `/api/ext-data/${id}/pull/run`,
      { method: 'POST' },
    ),

  // 内置预设 (概念/行业) 手动获取数据: 走结构转换, 保证 schema 一致
  extDataPresetFetch: (id: string) =>
    request<{ status: string; rows: number }>(
      `/api/ext-data/presets/${id}/fetch`,
      { method: 'POST' },
    ),

  extDataDetectFields: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<{ fields: { name: string; dtype: string; label: string }[]; rows: number; symbol_candidates: string[]; code_candidates: string[] }>(
      '/api/ext-data/detect-fields',
      { method: 'POST', body: fd },
    )
  },

  extDataDetectUrl: (body: ExtDataDetectUrlRequest) =>
    request<ExtDataDetectUrlResult>('/api/ext-data/detect-url', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  extDataFixSymbol: (id: string) =>
    request<{ status: string; fixed_files: number }>(
      `/api/ext-data/${id}/fix-symbol`,
      { method: 'POST' },
    ),

  // ===== Financials =====
  financialStatus: () =>
    request<FinancialStatus>('/api/financials/status'),

  financialMetrics: (symbol?: string) =>
    request<{ data: FinancialMetricRecord[] }>(
      `/api/financials/metrics${symbol ? `?symbol=${encodeURIComponent(symbol)}` : ''}`,
    ),

  financialIncome: (symbol?: string) =>
    request<{ data: FinancialIncomeRecord[] }>(
      `/api/financials/income${symbol ? `?symbol=${encodeURIComponent(symbol)}` : ''}`,
    ),

  financialBalanceSheet: (symbol?: string) =>
    request<{ data: FinancialBalanceSheetRecord[] }>(
      `/api/financials/balance-sheet${symbol ? `?symbol=${encodeURIComponent(symbol)}` : ''}`,
    ),

  financialCashFlow: (symbol?: string) =>
    request<{ data: FinancialCashFlowRecord[] }>(
      `/api/financials/cash-flow${symbol ? `?symbol=${encodeURIComponent(symbol)}` : ''}`,
    ),

  financialShares: (symbol?: string) =>
    request<{ data: FinancialSharesRecord[] }>(
      `/api/financials/shares${symbol ? `?symbol=${encodeURIComponent(symbol)}` : ''}`,
    ),

  /** 触发财务数据同步(后台异步执行,接口立即返回 started 状态) */
  financialSync: (table: string) =>
    request<{ status: string; synced: { started: boolean; reason?: string } }>(
      `/api/financials/sync/${table}`, { method: 'POST' },
    ),

  /** AI 分析报告 CRUD */
  financialReportsList: () =>
    request<{ reports: AiFinancialReport[] }>('/api/financials/reports'),

  financialReportSave: (r: {
    symbol: string; name?: string; focus?: string; content: string
    periods?: number; summary?: string
  }) =>
    request<{ ok: boolean; report: AiFinancialReport }>('/api/financials/reports', {
      method: 'POST', body: JSON.stringify(r),
    }),

  financialReportDelete: (reportId: string) =>
    request<{ ok: boolean }>(`/api/financials/reports/${encodeURIComponent(reportId)}`, { method: 'DELETE' }),

  /**
   * AI 财务分析 — 流式调用。
   *
   * 返回一个可逐行读取的 async generator,每行是 JSON:
   *   {type:"meta",symbol,summary,periods}
   *   {type:"delta",content:"..."}    ← 文本片段,逐个累加
   *   {type:"error",message:"..."}
   *   {type:"done"}
   *
   * 用 ReadableStream 解析(而非 SSE EventSource),支持 POST body 且更简单。
   */
  async *financialAnalyzeStream(symbol: string, focus?: string): AsyncGenerator<{
    type: 'meta' | 'delta' | 'error' | 'done'
    symbol?: string
    summary?: string
    periods?: number
    content?: string
    message?: string
  }> {
    const res = await fetch('/api/financials/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol, focus: focus ?? '' }),
    })
    if (!res.ok) {
      let detail = ''
      try { const j = JSON.parse(await res.text()); detail = j.detail ?? j.message ?? '' } catch { /* ignore */ }
      const msg = detail || `${res.status} ${res.statusText}`
      toast(msg, 'error')
      throw new Error(msg)
    }
    if (!res.body) throw new Error('响应无 body')

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      // 按行分割(保留最后不完整的行在 buf)
      const lines = buf.split('\n')
      buf = lines.pop() ?? ''
      for (const line of lines) {
        const s = line.trim()
        if (!s) continue
        try {
          yield JSON.parse(s)
        } catch {
          // 忽略无法解析的行
        }
      }
    }
    // 处理残余
    if (buf.trim()) {
      try { yield JSON.parse(buf.trim()) } catch { /* ignore */ }
    }
  },

  // ===== 个股分析 =====
  stockAnalysisLevels: (symbol: string, days = 120) =>
    request<StockLevels>(`/api/stock-analysis/levels?symbol=${encodeURIComponent(symbol)}&days=${days}`),

  /** 筹码分布(成本分布): 日K三角分布 + 换手衰减 */
  stockAnalysisChips: (symbol: string, params: { days?: number; bins?: number; decay?: number } = {}) => {
    const q = new URLSearchParams({ symbol })
    for (const [k, v] of Object.entries(params)) {
      if (v == null) continue
      q.set(k, String(v))
    }
    return request<ChipsDistribution>(`/api/stock-analysis/chips?${q.toString()}`)
  },

  stockAnalysisReportsList: () =>
    request<{ reports: AiStockReport[] }>('/api/stock-analysis/reports'),

  stockAnalysisReportSave: (r: {
    symbol: string; name?: string; focus?: string; content: string
    summary?: string; close?: number | null
    levels?: Record<LevelType, PriceLevel[]>
  }) =>
    request<{ ok: boolean; report: AiStockReport }>('/api/stock-analysis/reports', {
      method: 'POST', body: JSON.stringify(r),
    }),

  stockAnalysisReportDelete: (reportId: string) =>
    request<{ ok: boolean }>(`/api/stock-analysis/reports/${encodeURIComponent(reportId)}`, { method: 'DELETE' }),

  /**
   * AI 个股四维分析 — 流式调用(NDJSON,与财务分析同协议)。
   * meta 里额外带 levels(关键价位)供图表回放。
   */
  async *stockAnalyzeStream(symbol: string, focus?: string): AsyncGenerator<{
    type: 'meta' | 'delta' | 'error' | 'done'
    symbol?: string
    summary?: string
    levels?: Record<LevelType, PriceLevel[]>
    close?: number | null
    content?: string
    message?: string
  }> {
    const res = await fetch('/api/stock-analysis/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol, focus: focus ?? '' }),
    })
    if (!res.ok) {
      let detail = ''
      try { const j = JSON.parse(await res.text()); detail = j.detail ?? j.message ?? '' } catch { /* ignore */ }
      const msg = detail || `${res.status} ${res.statusText}`
      toast(msg, 'error')
      throw new Error(msg)
    }
    if (!res.body) throw new Error('响应无 body')

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const lines = buf.split('\n')
      buf = lines.pop() ?? ''
      for (const line of lines) {
        const s = line.trim()
        if (!s) continue
        try { yield JSON.parse(s) } catch { /* ignore */ }
      }
    }
    if (buf.trim()) {
      try { yield JSON.parse(buf.trim()) } catch { /* ignore */ }
    }
  },

  // ===== 大盘复盘 =====
  reviewReportsList: (market?: string) =>
    request<{ reports: AiReviewReport[] }>(`/api/market-recap/reports${market && market !== 'cn' ? `?market=${market}` : ''}`),

  reviewReportSave: (r: {
    as_of: string; focus?: string; content: string
    summary?: string; emotion_score?: number | null; emotion_label?: string
    market?: string
  }) =>
    request<{ ok: boolean; report: AiReviewReport }>('/api/market-recap/reports', {
      method: 'POST', body: JSON.stringify(r),
    }),

  reviewReportDelete: (reportId: string) =>
    request<{ ok: boolean }>(`/api/market-recap/reports/${encodeURIComponent(reportId)}`, { method: 'DELETE' }),

  /**
   * AI 大盘复盘 — 流式调用(NDJSON,与个股/财务分析同协议)。
   * meta 里带 as_of / emotion_score / emotion_label / summary,供前端先渲染信号灯。
   */
  async *reviewStream(asOf?: string, focus?: string, market?: string): AsyncGenerator<{
    type: 'meta' | 'delta' | 'error' | 'done'
    as_of?: string
    emotion_score?: number
    emotion_label?: string
    summary?: string
    content?: string
    message?: string
  }> {
    const res = await fetch('/api/market-recap/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ as_of: asOf ?? null, focus: focus ?? '', market: market ?? 'cn' }),
    })
    if (!res.ok) {
      let detail = ''
      try { const j = JSON.parse(await res.text()); detail = j.detail ?? j.message ?? '' } catch { /* ignore */ }
      const msg = detail || `${res.status} ${res.statusText}`
      toast(msg, 'error')
      throw new Error(msg)
    }
    if (!res.body) throw new Error('响应无 body')

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const lines = buf.split('\n')
      buf = lines.pop() ?? ''
      for (const line of lines) {
        const s = line.trim()
        if (!s) continue
        try { yield JSON.parse(s) } catch { /* ignore */ }
      }
    }
    if (buf.trim()) {
      try { yield JSON.parse(buf.trim()) } catch { /* ignore */ }
    }
  },

  /** AI 概念轮动分析 — 流式 NDJSON。 */
  async *rotationAnalyzeStream(days: number, focus?: string, kind?: 'concept' | 'industry', level?: number): AsyncGenerator<{
    type: 'meta' | 'delta' | 'error' | 'done'
    days?: number
    summary?: string
    content?: string
    message?: string
  }> {
    const res = await fetch('/api/rps/rotation-analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ days, focus: focus ?? '', kind: kind ?? 'concept', level: level ?? null }),
    })
    if (!res.ok) {
      let detail = ''
      try { const j = JSON.parse(await res.text()); detail = j.detail ?? j.message ?? '' } catch { /* ignore */ }
      const msg = detail || `${res.status} ${res.statusText}`
      toast(msg, 'error')
      throw new Error(msg)
    }
    if (!res.body) throw new Error('响应无 body')

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const lines = buf.split('\n')
      buf = lines.pop() ?? ''
      for (const line of lines) {
        const s = line.trim()
        if (!s) continue
        try { yield JSON.parse(s) } catch { /* ignore */ }
      }
    }
    if (buf.trim()) {
      try { yield JSON.parse(buf.trim()) } catch { /* ignore */ }
    }
  },

  // ===== Strategy Engine =====
  strategyList: (assetType?: 'stock' | 'etf', timeframe = '1d') => {
    const params = new URLSearchParams()
    if (assetType) params.set('asset_type', assetType)
    if (timeframe) params.set('timeframe', timeframe)
    const qs = params.toString()
    return request<{ strategies: StrategyDetail[]; load_errors?: StrategyLoadError[] }>(
      `/api/strategies${qs ? `?${qs}` : ''}`,
    )
  },

  strategyGet: (id: string) =>
    request<StrategyDetail>(`/api/strategies/${id}`),

  strategyRun: (strategyId: string, params?: Record<string, any>, asOf?: string, pool?: string[]) =>
    request<ScreenerResult>('/api/strategies/run', {
      method: 'POST',
      body: JSON.stringify({ strategy_id: strategyId, params, as_of: asOf ?? null, pool }),
    }),

  strategyRunAll: (asOf?: string) =>
    request<{ as_of: string | null; results: Record<string, { total: number; as_of: string }> }>(
      '/api/strategies/run-all',
      { method: 'POST', body: JSON.stringify({ as_of: asOf ?? null }) },
    ),

  strategySaveConfig: (strategyId: string, overrides: Record<string, any>) =>
    request<{ ok: boolean }>('/api/strategies/config', {
      method: 'POST',
      body: JSON.stringify({ strategy_id: strategyId, overrides }),
    }),

  strategyPatchConfig: (strategyId: string, overrides: Record<string, any>) =>
    request<{ ok: boolean }>('/api/strategies/config', {
      method: 'PATCH',
      body: JSON.stringify({ strategy_id: strategyId, overrides }),
    }),

  strategyResetConfig: (strategyId: string) =>
    request<{ ok: boolean }>(`/api/strategies/config/${strategyId}`, { method: 'DELETE' }),

  /** 删除自定义策略（内置策略不可删除） */
  strategyDelete: (strategyId: string) =>
    request<{ ok: boolean }>(`/api/strategies/${strategyId}`, { method: 'DELETE' }),

  strategyReload: () =>
    request<{ ok: boolean; count: number }>('/api/strategies/reload', { method: 'POST' }),

  // ===== Custom Signals (自定义信号) =====
  customSignalsList: () =>
    request<{ signals: CustomSignal[] }>('/api/custom-signals'),

  customSignalsOptions: () =>
    request<CustomSignalOptions>('/api/custom-signals/options'),

  customSignalSave: (signal: CustomSignal) =>
    request<{ ok: boolean; signal: CustomSignal }>('/api/custom-signals', {
      method: 'POST',
      body: JSON.stringify(signal),
    }),

  customSignalDelete: (id: string) =>
    request<{ ok: boolean }>(`/api/custom-signals/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  customSignalsAiGenerate: (description: string) =>
    request<CustomSignalAIGenerateResult>('/api/custom-signals/ai/generate', {
      method: 'POST',
      body: JSON.stringify({ description }),
    }),

  // ===== Abnormal Moves (异动边缘) =====
  abnormalOverview: (minCloseness = 0.5, limit = 200) =>
    request<AbnormalOverview>(
      `/api/abnormal/overview?min_closeness=${minCloseness}&limit=${limit}`,
    ),

  // ===== Monitor Rules (监控规则) =====
  monitorRulesList: () =>
    request<{ rules: MonitorRule[] }>('/api/monitor-rules'),

  monitorRuleOptions: () =>
    request<MonitorRuleOptions>('/api/monitor-rules/options'),

  monitorRuleSave: (rule: MonitorRule) =>
    request<{ ok: boolean; rule: MonitorRule }>('/api/monitor-rules', {
      method: 'POST',
      body: JSON.stringify(rule),
    }),

  monitorRuleDelete: (id: string) =>
    request<{ ok: boolean }>(`/api/monitor-rules/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  /** 模拟触发 ladder 封单监控 (Dev 调试, 不落盘不推送) */
  monitorRuleTestLadder: () =>
    request<{
      ok: boolean
      as_of: string
      sealed_count: number
      triggered: Array<{
        rule_id: string; rule_name: string; symbol: string; name?: string
        type: string; message: string; severity: string
        sealed_value: number; sealed_metric: string
        current_sealed_vol?: number; current_sealed_amount?: number
      }>
      not_triggered: Array<{
        rule_id: string; rule_name: string; symbol: string
        metric: string; threshold: number; current_value: number | null
        current_sealed_vol?: number; current_sealed_amount?: number | null
        reason: string
      }>
    }>('/api/monitor-rules/test-ladder', { method: 'POST' }),

  /** 真实触发 ladder 预警 (落盘+飞书+SSE), Dev 调试用 */
  monitorRuleTriggerLadder: () =>
    request<{
      ok: boolean
      triggered: number
      events: Array<{ symbol: string; name: string; message: string }>
    }>('/api/monitor-rules/trigger-ladder', { method: 'POST' }),

  /** 生成演示监控规则 (Dev 页用) */
  monitorRuleSeed: () =>
    request<{ ok: boolean; generated: number }>('/api/monitor-rules/seed', { method: 'POST' }),

  // ===== Alerts (触发记录) =====
  alertsList: (params?: { days?: number; limit?: number; source?: string; type?: string; extColumns?: string }) => {
    const qs = new URLSearchParams()
    if (params?.days) qs.set('days', String(params.days))
    if (params?.limit) qs.set('limit', String(params.limit))
    if (params?.source) qs.set('source', params.source)
    if (params?.type) qs.set('type', params.type)
    if (params?.extColumns) qs.set('ext_columns', params.extColumns)
    const s = qs.toString()
    return request<{ alerts: AlertEvent[]; total: number }>(`/api/alerts${s ? `?${s}` : ''}`)
  },

  /**
   * 单只股票的监控触发点(按天聚合), 供 K 线叠加。
   * 后端已把 UTC epoch 的 ts 按北京时间归日, 前端不重复换算。
   */
  alertsBySymbol: (symbol: string, days = 7) =>
    request<{ symbol: string; days: number; points: AlertTriggerPoint[] }>(
      `/api/alerts/by-symbol?symbol=${encodeURIComponent(symbol)}&days=${days}`),

  /**
   * 最近一次策略回测中该股票的买卖点, 供 K 线叠加。
   * 没跑过回测是正常状态 —— 后端返回空 trades, 不报错。
   */
  lastBacktestTrades: (symbol: string, limit = 200) =>
    request<{
      symbol: string
      strategy_id: string | null
      finished_at: string | null
      trades: BacktestTradeMark[]
    }>(`/api/backtest/trades/by-symbol?symbol=${encodeURIComponent(symbol)}&limit=${limit}`),

  alertsClear: () =>
    request<{ ok: boolean; cleared: number }>('/api/alerts', { method: 'DELETE' }),

  alertDelete: (ts: number) =>
    request<{ ok: boolean }>(`/api/alerts/${ts}`, { method: 'DELETE' }),

  /** 生成演示触发记录 (Dev 页用) */
  alertSeed: (count = 12, recent = true) =>
    request<{ ok: boolean; generated: number }>(`/api/alerts/seed?count=${count}&recent=${recent}`, { method: 'POST' }),

  /** 检查 AI 配置状态 */
  strategyAiStatus: () =>
    request<{ configured: boolean; has_key: boolean; has_model: boolean; provider?: string }>('/api/strategies/ai/status'),

  /** 测试 AI 连通性 */
  strategyAiTest: () =>
    request<{ ok: boolean; error?: string; model?: string; response?: string; usage?: { prompt: number; completion: number } }>(
      '/api/strategies/ai/test',
      { method: 'POST' },
    ),

  /** 获取策略源文件内容 */
  strategyGetSource: (id: string) =>
    request<{ code: string; source: string }>(`/api/strategies/${id}/source`),
  strategyBuild: (step: number, payload: Record<string, any>) =>
    request<StrategyBuildResult>(
      '/api/strategies/build',
      { method: 'POST', body: JSON.stringify({ step, ...payload }) },
    ),

  async *strategyBuildStream(step: number, payload: Record<string, any>): AsyncGenerator<StrategyBuildStreamEvent> {
    const res = await fetch('/api/strategies/build/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ step, ...payload }),
    })
    if (!res.ok) {
      let detail = ''
      try { const j = JSON.parse(await res.text()); detail = j.detail ?? j.message ?? '' } catch { /* ignore */ }
      const msg = detail || `${res.status} ${res.statusText}`
      toast(msg, 'error')
      throw new Error(msg)
    }
    if (!res.body) throw new Error('响应无 body')

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const lines = buf.split('\n')
      buf = lines.pop() ?? ''
      for (const line of lines) {
        const s = line.trim()
        if (!s) continue
        try { yield JSON.parse(s) } catch { /* ignore */ }
      }
    }
    if (buf.trim()) {
      try { yield JSON.parse(buf.trim()) } catch { /* ignore */ }
    }
  },

  strategyValidateCode: (payload: { code: string; strategy_id?: string; name?: string; description?: string }) =>
    request<StrategyBuildResult>('/api/strategies/code/validate', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  strategySaveCodeV2: (payload: {
    strategy_id: string
    code: string
    target_source: 'ai' | 'custom'
    mode: 'create' | 'update'
    name?: string
    description?: string
  }) =>
    request<StrategyCodeSaveResult>('/api/strategies/code/save', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  /** 创建/更新叠加策略(composite): 声明式引用多个子策略 */
  strategySaveComposite: (payload: {
    strategy_id: string
    name: string
    description?: string
    children: { strategy_id: string; weight: number }[]
    merge_mode: 'union' | 'intersect'
    min_confirm?: number
    mode: 'create' | 'update'
  }) =>
    request<StrategyCodeSaveResult>('/api/strategies/composite/save', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  /** 保存 AI 生成的策略文件 */
  strategySaveCode: (strategyId: string, code: string, meta?: { name?: string; description?: string }) =>
    request<{ ok: boolean; path: string }>('/api/strategies/ai/save', {
      method: 'POST',
      body: JSON.stringify({ strategy_id: strategyId, code, name: meta?.name ?? '', description: meta?.description ?? '' }),
    }),

  // ===== Signal Lab (信号实验室) =====
  signalLabStrategies: () =>
    request<SignalLabStrategies>('/api/signallab/strategies'),

  signalLabDatasets: (strategyId?: string) =>
    request<{ datasets: SignalLabDataset[] }>(
      `/api/signallab/datasets${strategyId ? `?strategy_id=${encodeURIComponent(strategyId)}` : ''}`,
    ),

  signalLabRun: (payload: SignalLabRunRequest) =>
    request<SignalLabRunStart>('/api/signallab/runs', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  signalLabRunStatus: (runId: string) =>
    request<SignalLabRun>(`/api/signallab/runs/${encodeURIComponent(runId)}`),

  signalLabOutcomes: (params: {
    strategy_id: string
    start?: string
    end?: string
    symbol?: string
    filled_only?: boolean
    limit?: number
    offset?: number
    sort?: 'signal_date' | 'fill_date' | 'symbol'
    descending?: boolean
  }) => {
    const q = new URLSearchParams({ strategy_id: params.strategy_id })
    for (const [k, v] of Object.entries(params)) {
      if (k === 'strategy_id' || v == null) continue
      q.set(k, String(v))
    }
    return request<SignalLabOutcomes>(`/api/signallab/outcomes?${q.toString()}`)
  },

  signalLabSummary: (params: {
    strategy_id: string
    start?: string
    end?: string
    horizons?: string
    group_by?: string
  }) => {
    const q = new URLSearchParams({ strategy_id: params.strategy_id })
    for (const [k, v] of Object.entries(params)) {
      if (k === 'strategy_id' || v == null) continue
      q.set(k, String(v))
    }
    return request<SignalLabSummary>(`/api/signallab/summary?${q.toString()}`)
  },

  signalLabAttribution: (params: {
    strategy_id: string
    start?: string
    end?: string
    features?: string
    horizon?: number
    min_samples?: number
    buckets?: number
  }) => {
    const q = new URLSearchParams({ strategy_id: params.strategy_id })
    for (const [k, v] of Object.entries(params)) {
      if (k === 'strategy_id' || v == null) continue
      q.set(k, String(v))
    }
    return request<SignalLabAttribution>(`/api/signallab/attribution?${q.toString()}`)
  },

  /** 当日候选打分: 把归因学到的档位搬到今天, 只改排序不改选股结果 */
  signalLabScoreToday: (params: {
    strategy_id: string
    as_of?: string
    start?: string
    end?: string
    horizon?: number
    min_samples?: number
    buckets?: number
    limit?: number
  }) => {
    const q = new URLSearchParams({ strategy_id: params.strategy_id })
    for (const [k, v] of Object.entries(params)) {
      if (k === 'strategy_id' || v == null) continue
      q.set(k, String(v))
    }
    return request<SignalLabScoreToday>(`/api/signallab/score-today?${q.toString()}`)
  },

  /**
   * AI 形态归因解读 (NDJSON 流)。
   * 每帧: meta / delta / error / suggestions / done —— suggestions 是正文流完后
   * 补发的机器可读参数建议(已按策略参数声明收口), 可直接送进参数网格搜索。
   */
  signalLabInsight: async (
    payload: {
      strategy_id: string
      start?: string
      end?: string
      horizon?: number
      min_samples?: number
      buckets?: number
      focus?: string
    },
    onEvent: (event: SignalLabInsightEvent) => void,
    signal?: AbortSignal,
  ) => {
    const resp = await fetch('/api/signallab/attribution-insight', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal,
    })
    if (!resp.ok || !resp.body) throw new Error(`AI 解读请求失败 (${resp.status})`)
    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''
      for (const line of lines) {
        if (!line.trim()) continue
        try {
          onEvent(JSON.parse(line) as SignalLabInsightEvent)
        } catch {
          // 半包/脏行直接跳过, 不打断整条流
        }
      }
    }
  },

  // ===== Pulse (盘中脉搏: 资金流/竞价/力道/题材/梯队/逐笔) =====
  pulseMoneyflow: (symbols: string[], days = 5) =>
    request<PulseMoneyflowResp>(
      `/api/pulse/moneyflow?symbols=${encodeURIComponent(symbols.join(','))}&days=${days}`,
    ),

  pulseMoneyflowRank: (params: { limit?: number; days?: number; scan?: number; ascending?: boolean }) => {
    const q = new URLSearchParams()
    for (const [k, v] of Object.entries(params)) {
      if (v == null) continue
      q.set(k, String(v))
    }
    return request<PulseMoneyflowRankResp>(`/api/pulse/moneyflow/rank?${q.toString()}`)
  },

  pulseAuction: (symbol: string) =>
    request<PulseAuction>(`/api/pulse/auction?symbol=${encodeURIComponent(symbol)}`),

  pulseAuctionScan: (params: {
    scan?: number
    mode?: 'score' | 'repair'
    limit?: number
    ascending?: boolean
    with_snapshot?: boolean
    min_open_pct?: number
    max_open_pct?: number
  }) => {
    const q = new URLSearchParams()
    for (const [k, v] of Object.entries(params)) {
      if (v == null) continue
      q.set(k, String(v))
    }
    return request<PulseAuctionScanResp>(`/api/pulse/auction/scan?${q.toString()}`)
  },

  pulseStrength: (symbol: string) =>
    request<PulseStrength>(`/api/pulse/strength?symbol=${encodeURIComponent(symbol)}`),

  pulseTopics: (symbol: string) =>
    request<PulseTopics>(`/api/pulse/topics?symbol=${encodeURIComponent(symbol)}`),

  pulseTopicRank: (params: { include_pseudo?: boolean; force?: boolean } = {}) => {
    const q = new URLSearchParams()
    for (const [k, v] of Object.entries(params)) {
      if (v == null) continue
      q.set(k, String(v))
    }
    return request<PulseTopicRank>(`/api/pulse/topics/rank?${q.toString()}`)
  },

  pulseLadder: (params: { min_level?: number; only_sealed?: boolean; limit?: number; force?: boolean } = {}) => {
    const q = new URLSearchParams()
    for (const [k, v] of Object.entries(params)) {
      if (v == null) continue
      q.set(k, String(v))
    }
    return request<PulseLadder>(`/api/pulse/ladder?${q.toString()}`)
  },

  pulseTicks: (symbol: string, params: { day?: string; limit?: number; force?: boolean } = {}) => {
    const q = new URLSearchParams({ symbol })
    for (const [k, v] of Object.entries(params)) {
      if (v == null) continue
      q.set(k, String(v))
    }
    return request<PulseTicks>(`/api/pulse/ticks?${q.toString()}`)
  },

  pulseOrderflow: (symbol: string, params: { day?: string; force?: boolean } = {}) => {
    const q = new URLSearchParams({ symbol })
    for (const [k, v] of Object.entries(params)) {
      if (v == null) continue
      q.set(k, String(v))
    }
    return request<PulseOrderflow>(`/api/pulse/orderflow?${q.toString()}`)
  },

  pulseFootprint: (symbol: string, params: { day?: string; rows?: number; bucket?: number; force?: boolean } = {}) => {
    const q = new URLSearchParams({ symbol })
    for (const [k, v] of Object.entries(params)) {
      if (v == null) continue
      q.set(k, String(v))
    }
    return request<PulseFootprint>(`/api/pulse/footprint?${q.toString()}`)
  },

  pulsePriceDist: (symbol: string, params: { day?: string; rows?: number; force?: boolean } = {}) => {
    const q = new URLSearchParams({ symbol })
    for (const [k, v] of Object.entries(params)) {
      if (v == null) continue
      q.set(k, String(v))
    }
    return request<PulsePriceDist>(`/api/pulse/price-dist?${q.toString()}`)
  },

  pulseTickDays: () => request<{ days: string[] }>('/api/pulse/tick-days'),

  // ===== 飞书推送 =====
  larkStatus: () => request<LarkStatus>('/api/lark/status'),

  larkTables: () => request<{ tables: LarkTableOption[] }>('/api/lark/tables'),

  larkPush: (params: {
    strategy_id: string
    as_of?: string
    force?: boolean
    dry_run?: boolean
    min_closeness?: number
    with_momentum?: boolean
    market?: string
  }) =>
    request<LarkPushResult>('/api/lark/push', {
      method: 'POST',
      body: JSON.stringify(params),
    }),
}

// ===== Pipeline =====
export interface PipelineJob {
  id: string
  status: 'pending' | 'running' | 'succeeded' | 'failed'
  stage: string
  progress: number          // 0-100 整体进度
  stage_pct: number         // 0-100 当前阶段内进度
  log: { ts: string; stage: string; msg: string }[]
  started_at: string | null
  finished_at: string | null
  duration_s: number | null
  result: {
    universe_size: number
    daily_days: number
    adj_factor_symbols: number
    enriched_days: number
    index_count?: number
    index_daily_rows?: number
    minute_rows: number
    skipped_stages?: string[]
  } | null
  error: string | null
}

export type PipelineJobSummary = Omit<PipelineJob, 'log'>

// ===== Data status =====
interface TableStats {
  rows: number
  earliest_date: string | null
  latest_date: string | null
  symbols_covered: number
  trading_days: number
}

interface InstrumentsStats {
  rows: number
  symbols_covered: number
  latest_as_of: string | null
  named: number
}

export interface DataStatus {
  daily: TableStats | null
  enriched: TableStats | null
  index_daily: TableStats | null
  index_enriched: TableStats | null
  index_instruments: InstrumentsStats | null
  etf_daily: TableStats | null
  etf_enriched: TableStats | null
  etf_instruments: InstrumentsStats | null
  minute: TableStats | null
  adj_factor: TableStats | null
  instruments: InstrumentsStats | null
  financials: { rows: number; tables: Record<string, { rows: number; symbols: number }> } | null
  storage: {
    daily_files: number
    daily_size_mb: number
    enriched_files: number
    enriched_size_mb: number
    index_daily_files?: number
    index_daily_size_mb?: number
    index_enriched_files?: number
    index_enriched_size_mb?: number
    index_instruments_files?: number
    index_instruments_size_mb?: number
    etf_daily_files?: number
    etf_daily_size_mb?: number
    etf_enriched_files?: number
    etf_enriched_size_mb?: number
    etf_instruments_files?: number
    etf_instruments_size_mb?: number
    etf_adj_factor_files?: number
    etf_adj_factor_size_mb?: number
    minute_files: number
    minute_size_mb: number
    adj_factor_files: number
    adj_factor_size_mb: number
    instruments_files: number
    instruments_size_mb: number
    financials_files?: number
    financials_size_mb?: number
    ext_data_files?: number
    ext_data_size_mb?: number
    total_size_mb: number
  }
  next_pipeline_run: string | null
  next_instruments_run: string | null
  last_pipeline_run: string | null
  last_instruments_run: string | null
  checked_at: string
  indicators_ready?: boolean
}

export interface EnrichedField {
  name: string
  type: string
  desc: string
}

// ===== 扩展数据 =====
export interface ExtDataField {
  name: string
  dtype: string
  label: string
}

export interface PullConfig {
  url: string
  method: string
  headers?: Record<string, string>
  body?: string | null
  response_path: string
  field_map?: Record<string, string>
  schedule_minutes: number
  enabled: boolean
  last_run?: string | null
  last_status?: string | null
  last_message?: string | null
  last_rows?: number | null
  next_run?: string | null
  time_window_start?: string | null
  time_window_end?: string | null
}

export interface ExtDataDetectUrlRequest {
  url: string
  method?: string
  headers?: Record<string, string>
  body?: string
  response_path?: string
  field_map?: Record<string, string>
}

export interface ExtDataDetectUrlResult {
  status: string
  total_rows: number
  response_path: string
  response_path_candidates: string[]
  fields: ExtDataField[]
  symbol_candidates: string[]
  code_candidates: string[]
  preview: Record<string, unknown>[]
}

export interface ExtDataConfig {
  id: string
  label: string
  mode: 'snapshot' | 'timeseries'
  fields: ExtDataField[]
  description?: string
  symbol_map?: Record<string, string>
  code_map?: Record<string, string>
  created_at: string
  updated_at: string
  latest_sync_date?: string | null
  date_range?: string[] | null
  pull?: PullConfig | null
}

export interface ExtDataRowsResult {
  id: string
  label: string
  mode: 'snapshot' | 'timeseries'
  date: string | null
  total: number
  limit: number
  fields: ExtDataField[]
  rows: Record<string, any>[]
}

export interface DimensionMembersResult {
  id: string
  label: string
  date: string | null
  field: string
  value: string
  total: number
  limit: number
  rows: Record<string, any>[]
}

export interface AnalysisColumn {
  field: string
  label?: string
  type?: 'string' | 'number' | 'percent' | 'amount' | 'date'
  width?: number | null
  sortable?: boolean
  precision?: number | null
  format?: string | null
  aggregate?: 'count' | 'avg' | 'sum' | 'min' | 'max' | null
  visible?: boolean
}

export interface AnalysisMenu {
  id: string
  label: string
  icon: string
  data_source: string
  template: 'dimension_rank' | 'ranking' | 'table'
  dimension_field?: string | null
  rank_field?: string | null
  group_columns: AnalysisColumn[]
  detail_columns: AnalysisColumn[]
  default_sort?: { field: string; order: 'asc' | 'desc' } | null
  visible: boolean
  order: number
  created_at?: string | null
  updated_at?: string | null
  builtin?: boolean
}

// ===== Pulse (盘中脉搏) 类型 =====
// 统一量纲: 金额=元, 比率=小数(0.0968 即 9.68%), 成交量=手。

/** M1 资金流逐日记录。 */
export interface PulseMoneyflowRow {
  symbol: string
  date: string
  total_amount: number | null
  main_net: number | null
  main_ratio: number | null
  super_large_net: number | null
  large_net: number | null
  medium_net: number | null
  small_net: number | null
  buy_net: number | null
  buy_ratio: number | null
  buckets?: number[]
}

export interface PulseMoneyflowResp {
  symbols: string[]
  days: number
  rows: PulseMoneyflowRow[]
}

/** M1 排行行: 多日压缩后的一行。 */
export interface PulseMoneyflowRankRow {
  symbol: string
  name?: string
  date: string
  main_net: number | null
  main_ratio: number | null
  total_amount: number | null
  super_large_net: number | null
  large_net: number | null
  medium_net: number | null
  small_net: number | null
  sum_main_net: number | null
  inflow_days: number
  days: number
}

export interface PulseMoneyflowRankResp {
  scanned: number
  days: number
  rows: PulseMoneyflowRankRow[]
}

/** M2 集合竞价。 */
export interface PulseAuction {
  symbol: string
  date: string
  pre_close: number | null
  open_price: number | null
  open_volume: number | null
  open_amount: number | null
  open_change_pct: number | null
  points: { time: string; price: number | null; matched: number | null; unmatched: number | null }[]
  score: {
    total: number
    accel: number | null
    cancel_rate: number | null
    stability: number | null
    open_turnover_bp: number | null
    parts: { accel: number; cancel: number; stability: number; volume: number }
  } | null
}

/** M2 全市场竞价扫描行。score 为 null = 竞价序列点数不足(<6), 不参与强度排序。 */
export interface PulseAuctionScanRow {
  symbol: string
  name?: string
  date: string
  pre_close: number | null
  open_price: number | null
  open_change_pct: number | null
  open_volume: number | null
  open_amount: number | null
  n_points: number
  score: number | null
  accel: number | null
  cancel_rate: number | null
  stability: number | null
  open_turnover_bp: number | null
  // 合并快照后才有(低开走强榜依赖它)
  last_price?: number | null
  change_pct?: number | null
  repair_pct?: number | null
  is_limit_up?: boolean
}

export interface PulseAuctionScanResp {
  date: string
  scanned: number
  scored: number
  mode: string
  snapshot_ts: number | null
  rows: PulseAuctionScanRow[]
}

/** M3 分时买卖力道。 */
export interface PulseStrength {
  symbol: string
  points: { minute: string; buy: number; sell: number; delta: number; cum_delta: number }[]
  summary: {
    buy_total: number
    sell_total: number
    net: number
    strength_ratio: number
    tail_net: number
    peak_minute: string
    peak_delta: number
    minutes: number
  } | null
}

/** M4 个股题材。 */
export interface PulseTopics {
  symbol: string
  topics: {
    topic_id: string
    topic_name: string
    relation_level: number | null
    selected_date: string
    reason: string
  }[]
}

/** M4 题材强度排行。stale=true 表示返回的是旧值(后端正在后台刷新)。 */
export interface PulseTopicRank {
  trade_date: string | null
  rows: {
    rank: number | null
    topic_id: string
    topic_name: string
    limit_up_count: number | null
    highest_ladder_level: number | null
    lianban_count: number | null
    total_seal_amount: number | null
    leader_symbol: string | null
    leader_name: string | null
    leader_ladder_level: number | null
  }[]
  stale: boolean
  stamp: number | null
  filtered_pseudo: boolean
}

/** M5 涨停梯队。 */
export interface PulseLadder {
  trade_date: string | null
  rows: {
    symbol: string
    name: string | null
    limit_status: string
    ladder_level: number | null
    limit_board_text: string
    limit_up_streak_days: number | null
    year_limit_up_days: number | null
    open_price: number | null
    pre_close: number | null
    open_change_pct: number | null
    open_amount: number | null
    open_volume_hand: number | null
    seal_amount: number | null
    seal_to_amount_ratio: number | null
    seal_to_float_ratio: number | null
    pe_ttm: number | null
    beta_60d: number | null
    float_market_value: number | null
  }[]
  total: number
  stale: boolean
  stamp: number | null
  stats: {
    count: number
    sealed: number
    seal_total_amount: number
    max_level: number
    by_level: Record<string, number>
  }
}

/** M6 逐笔明细。 */
export interface PulseTicks {
  symbol: string
  day: string
  total: number
  rows: {
    index: number | null
    time: string
    price: number
    volume: number
    order_count: number | null
    side: string
    kind: string
  }[]
  updated: string | null
}

/** M6 订单流。 */
export interface PulseOrderflow {
  symbol: string
  day: string
  points: {
    minute: string
    buy: number
    sell: number
    delta: number
    cum_delta: number
    volume: number
    price: number
    trades: number
  }[]
  summary: {
    trades: number
    buy_volume: number
    sell_volume: number
    neutral_volume: number
    delta: number
    delta_ratio: number
    avg_price: number
  } | null
}

// ===== 飞书推送 =====
/** 可推送的目标: 策略 id 或 abnormal(异动预警)。configured=false 表示飞书表还没建。 */
export interface LarkTableOption {
  id: string
  label: string
  configured: boolean
}

export interface LarkPushResult {
  ok: boolean
  strategy_id: string
  label: string
  as_of: string | null
  selected: number       // 该日期选中的个股数
  pushed: number         // 实际写入条数
  skipped: number        // 去重跳过条数
  details: string[]
  error: string | null
  dry_run: boolean
  sample?: Record<string, unknown>[]   // 仅 dry_run
}

export interface LarkStatus {
  cli: string
  available: boolean
}

/** M6 足迹图: 价格(纵) x 时间(横) 的买卖量网格。 */
export interface PulseFootprint {
  symbol: string
  day: string
  price_levels: number[]
  time_buckets: string[]
  cells: { t: string; p: number; buy: number; sell: number; delta: number; volume: number }[]
  poc: number | null
  vwap: number | null
  low: number
  high: number
  step: number
  bucket_minutes: number
}

export interface PulsePriceDistRow {
  /** 档中值 */
  price: number
  /** 档区间 [low, high) */
  low: number
  high: number
  /** 手 */
  volume: number
  /** 元 */
  amount: number
  trades: number
  /** 主动买 / 主动卖 / 竞价撮合, 手 */
  buy: number
  sell: number
  neutral: number
  /** 占全日成交量比例(小数), cum_ratio 从最低价档往上累加 */
  ratio: number
  cum_ratio: number
}

export interface PulsePriceDist {
  symbol: string
  day: string
  updated: string | null
  rows: PulsePriceDistRow[]
  /** 成交最密集价(POC) */
  poc: number | null
  vwap: number | null
  /** 最新一笔成交价 */
  current: number | null
  low: number
  high: number
  step: number
  total_volume: number
  total_amount: number
  buy_volume: number
  sell_volume: number
  neutral_volume: number
}
