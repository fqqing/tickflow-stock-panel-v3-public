/**
 * KLineChart 内核的指标清单与持久化（S2 主副图窗格）。
 *
 * ★ 默认参数不在这里硬编码 —— klinecharts 内置的 27 个指标各有默认 calcParams,
 *   硬编码既容易记错(比如 CCI 是 13 还是 14)也会随库版本漂移。
 *   这里的做法是: 创建时**不传** calcParams(让库用默认), 再从 getIndicators()
 *   回读真实值写回状态(见 KLinePro.applyIndicators)。所以 params 为空数组
 *   表示"跟随库默认", 而不是"零参数"。
 *
 * 持久化两份东西:
 *   - 指标列表(按 key 增删/改参), 全局共享, 不按股票区分 —— 终端是工作台, 换股不该换指标。
 *   - 副图窗格高度(拖过分隔条才记), 按副图顺序存数组, 因为 paneId 是库内自增值,
 *     重建图表后会变, 不能用它当 key。
 */

export type IndicatorGroup = 'main' | 'sub'

export interface IndicatorMeta {
  /** klinecharts 内置指标名, 与 getSupportedIndicators() 对齐 */
  name: string
  cn: string
  group: IndicatorGroup
  /** 参数标签; 数量不足时用「参数N」兜底 */
  labels?: string[]
}

/**
 * 内置指标清单(getSupportedIndicators() 实测返回 27 个, 全部登记)。
 * 分组只影响创建时挂到哪个窗格: main -> candle_pane(叠在主图), sub -> 新建副图窗格。
 */
export const INDICATOR_METAS: IndicatorMeta[] = [
  // ── 主图(价格类, 与 K 线同窗格) ──
  { name: 'MA', cn: '均线', group: 'main', labels: ['周期', '周期', '周期', '周期', '周期', '周期'] },
  { name: 'EMA', cn: '指数均线', group: 'main', labels: ['周期', '周期', '周期', '周期', '周期', '周期'] },
  { name: 'SMA', cn: '平滑均线', group: 'main', labels: ['周期', '权重'] },
  { name: 'BOLL', cn: '布林带', group: 'main', labels: ['周期', '倍数'] },
  { name: 'BBI', cn: '多空均线', group: 'main', labels: ['周期', '周期', '周期', '周期'] },
  { name: 'SAR', cn: '抛物线转向', group: 'main', labels: ['起始', '步长', '极值'] },
  { name: 'AVP', cn: '平均价格', group: 'main', labels: ['周期'] },
  // ── 副图 ──
  { name: 'VOL', cn: '成交量', group: 'sub', labels: ['均线周期', '均线周期', '均线周期'] },
  { name: 'MACD', cn: '指数平滑异同', group: 'sub', labels: ['快线', '慢线', '信号'] },
  { name: 'KDJ', cn: '随机指标', group: 'sub', labels: ['周期', 'K', 'D'] },
  { name: 'RSI', cn: '相对强弱', group: 'sub', labels: ['周期', '周期', '周期'] },
  { name: 'WR', cn: '威廉指标', group: 'sub', labels: ['周期', '周期', '周期'] },
  { name: 'BIAS', cn: '乖离率', group: 'sub', labels: ['周期', '周期', '周期'] },
  { name: 'CCI', cn: '顺势指标', group: 'sub', labels: ['周期'] },
  { name: 'DMI', cn: '趋向指标', group: 'sub', labels: ['周期', '平滑'] },
  { name: 'CR', cn: '能量指标', group: 'sub', labels: ['周期', '周期', '周期', '周期', '周期'] },
  { name: 'PSY', cn: '心理线', group: 'sub', labels: ['周期', '均线周期'] },
  { name: 'DMA', cn: '平行线差', group: 'sub', labels: ['短周期', '长周期', '均线周期'] },
  { name: 'TRIX', cn: '三重平滑', group: 'sub', labels: ['周期', '均线周期'] },
  { name: 'OBV', cn: '能量潮', group: 'sub', labels: ['均线周期'] },
  { name: 'VR', cn: '成交量比率', group: 'sub', labels: ['周期', '均线周期'] },
  { name: 'EMV', cn: '简易波动', group: 'sub', labels: ['周期', '均线周期'] },
  { name: 'MTM', cn: '动量指标', group: 'sub', labels: ['周期', '均线周期'] },
  { name: 'ROC', cn: '变动率', group: 'sub', labels: ['周期', '均线周期'] },
  { name: 'PVT', cn: '价量趋势', group: 'sub', labels: ['周期'] },
  { name: 'BRAR', cn: '情绪指标', group: 'sub', labels: ['周期'] },
  { name: 'AO', cn: '动量震荡', group: 'sub', labels: ['短周期', '长周期'] },
]

const META_BY_NAME = new Map(INDICATOR_METAS.map(m => [m.name, m]))

export function metaOf(name: string): IndicatorMeta | undefined {
  return META_BY_NAME.get(name)
}

export function isSupported(name: string): boolean {
  return META_BY_NAME.has(name)
}

/** 主图窗格的固定 id(klinecharts 内置常量, createOverlay 也用它) */
export const MAIN_PANE_ID = 'candle_pane'

export interface IndicatorConfig {
  /** 本地唯一键; 同名指标可加多个(不同参数) */
  key: string
  name: string
  group: IndicatorGroup
  /** 空数组 = 用库默认值 */
  params: number[]
}

export const DEFAULT_INDICATORS: IndicatorConfig[] = [
  { key: 'ma#1', name: 'MA', group: 'main', params: [] },
  { key: 'vol#1', name: 'VOL', group: 'sub', params: [] },
]

const LS_INDICATORS = 'tickflow.kline.indicators.v1'
const LS_PANE_HEIGHTS = 'tickflow.kline.paneHeights.v1'

function readJSON<T>(key: string): T | null {
  try {
    const raw = window.localStorage.getItem(key)
    if (!raw) return null
    return JSON.parse(raw) as T
  } catch {
    return null
  }
}

function writeJSON(key: string, value: unknown): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(value))
  } catch {
    // 隐私模式 / 配额满: 静默降级成"本次会话有效"
  }
}

function sanitize(raw: unknown): IndicatorConfig[] | null {
  if (!Array.isArray(raw)) return null
  const out: IndicatorConfig[] = []
  for (const it of raw as Record<string, unknown>[]) {
    if (!it || typeof it.name !== 'string') continue
    if (!isSupported(it.name)) continue // 指标被库移除 / 脏数据
    const group: IndicatorGroup = it.group === 'main' ? 'main' : 'sub'
    const params = Array.isArray(it.params) ? it.params.filter(v => typeof v === 'number' && Number.isFinite(v)) : []
    out.push({ key: typeof it.key === 'string' && it.key ? it.key : '', name: it.name, group, params })
  }
  if (out.length === 0) return null
  // 补 key 并去重(key 冲突时按 name#n 重新编号)
  const seen = new Set<string>()
  const result: IndicatorConfig[] = []
  for (const c of out) {
    let key = c.key
    if (!key || seen.has(key)) {
      const base = c.name.toLowerCase()
      let i = 1
      while (seen.has(`${base}#${i}`)) i += 1
      key = `${base}#${i}`
    }
    seen.add(key)
    result.push({ ...c, key })
  }
  return result
}

export function loadIndicators(): IndicatorConfig[] {
  const parsed = sanitize(readJSON<unknown>(LS_INDICATORS))
  return parsed ?? DEFAULT_INDICATORS
}

export function saveIndicators(list: IndicatorConfig[]): void {
  writeJSON(LS_INDICATORS, list)
}

/** 同名指标可共存: key 取 name#n, n 从 1 递增 */
export function makeKey(list: IndicatorConfig[], name: string): string {
  const base = name.toLowerCase()
  const used = new Set(list.map(c => c.key))
  let i = 1
  while (used.has(`${base}#${i}`)) i += 1
  return `${base}#${i}`
}

/** 副图高度(按副图从上到下顺序); 没拖过就是空数组 */
export function loadPaneHeights(): number[] {
  const raw = readJSON<unknown>(LS_PANE_HEIGHTS)
  if (!Array.isArray(raw)) return []
  return (raw as unknown[]).filter((v): v is number => typeof v === 'number' && Number.isFinite(v) && v > 0)
}

export function savePaneHeights(heights: number[]): void {
  writeJSON(LS_PANE_HEIGHTS, heights)
}
