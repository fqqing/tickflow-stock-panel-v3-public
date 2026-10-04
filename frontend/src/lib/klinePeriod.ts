/**
 * K 线周期 / 复权的共享定义 —— 两个图表内核的唯一档位来源。
 *
 * ★ 为什么单独拆一个文件: 此前 ECharts 侧(StockDailyKChart)写了一份
 *   PERIOD_OPTIONS(有 90m/120m、无 1m), KLineChart 侧(KLinePro)另写一份
 *   PERIOD_TABS(有 1m、无 90m/120m)。同一个「周期」概念两处实现, 切内核时
 *   档位凭空多/少, 是「切换割裂」最直观的来源之一。
 *   现在两边都消费 KLINE_PERIOD_TABS, 新增档位只改这里。
 *
 * 1 分钟档的行数很大(单日 240 根), ECharts 渲染会掉帧, 但这是**渲染能力**
 * 差异而不是档位差异 —— 用 `PERIOD_CAPABILITY` 声明, UI 按能力过滤,
 * 不要各自维护一份子集。
 */

export type KLinePeriod =
  | 'day' | 'week' | 'month'
  | '1m' | '5m' | '15m' | '30m' | '60m' | '90m' | '120m'

const MINUTE_PERIODS: KLinePeriod[] = ['1m', '5m', '15m', '30m', '60m', '90m', '120m']

/** 分钟档由后端用 1 分钟 K 聚合(依赖分钟数据是否回补); 日/周/月走 /kline/daily */
export const isMinutePeriod = (p: KLinePeriod): boolean => MINUTE_PERIODS.includes(p)

export interface PeriodTab {
  key: KLinePeriod
  label: string
}

/** 全档位表(顺序即工具条顺序) */
export const KLINE_PERIOD_TABS: PeriodTab[] = [
  { key: 'day', label: '日' },
  { key: 'week', label: '周' },
  { key: 'month', label: '月' },
  { key: '1m', label: '1分' },
  { key: '5m', label: '5分' },
  { key: '15m', label: '15分' },
  { key: '30m', label: '30分' },
  { key: '60m', label: '60分' },
  { key: '90m', label: '90分' },
  { key: '120m', label: '120分' },
]

/**
 * 内核能力位: UI 按这个过滤档位, 不按内核名分叉。
 * ECharts 不列 1m(渲染掉帧), KLineChart 全档位都吃得下。
 */
export interface PeriodCapability {
  /** 是否支持 1 分钟档 */
  minute1: boolean
}

export const PERIOD_CAPABILITY: Record<'echarts' | 'klinecharts', PeriodCapability> = {
  echarts: { minute1: false },
  klinecharts: { minute1: true },
}

export function periodTabsFor(kernel: keyof typeof PERIOD_CAPABILITY): PeriodTab[] {
  const cap = PERIOD_CAPABILITY[kernel]
  return KLINE_PERIOD_TABS.filter(t => (t.key === '1m' ? cap.minute1 : true))
}

/**
 * 周期按钮的 tooltip: 说明这一档的聚合口径。
 *
 * ★ 原先这段文案在 StockDailyKChart 里写成「day / week / 其它」的三元表达式,
 *   于是 6 个分钟档的 tooltip 全部显示「月K(按月聚合并重算指标)」。档位既然
 *   已经统一到这里, 文案也一并收口 —— 新增档位不会再漏掉 tooltip。
 */
export function periodTabTitle(t: PeriodTab): string {
  if (t.key === 'day') return '日K'
  if (t.key === 'week') return '周K(按周聚合并重算指标)'
  if (t.key === 'month') return '月K(按月聚合并重算指标)'
  return `${t.label}K(按${t.label}聚合并重算指标)`
}

/** 分钟周期 -> klinecharts span */
export const MINUTE_SPAN: Record<string, number> = {
  '1m': 1, '5m': 5, '15m': 15, '30m': 30, '60m': 60, '90m': 90, '120m': 120,
}

/** 复权方式: qfq(前复权, 默认) / none(不复权) / hfq(后复权) */
export type KLineAdjust = 'qfq' | 'none' | 'hfq'

export interface AdjustOption {
  key: KLineAdjust
  label: string
  title: string
}

export const ADJUST_OPTIONS: AdjustOption[] = [
  { key: 'qfq', label: '前复权', title: '前复权: 以最新价为基准, 历史价向下调整(消除除权跳空)' },
  { key: 'none', label: '不复权', title: '不复权: 交易所真实成交价, 除权日会保留跳空' },
  { key: 'hfq', label: '后复权', title: '后复权: 以最早价为基准, 历史价显示为真实价' },
]
