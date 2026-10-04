/**
 * 渲染器契约 —— 让「切内核」退化成换渲染器, 而不是换一棵组件树。
 *
 * ★ 为什么需要它: 此前终端层是按内核名分叉的 —— 「1m 按钮只给 KLinePro、
 *   手绘线只给 ECharts」这类判断散落在两套组件里。结果就是每加一个能力,
 *   要在多处写 `useKLine ? ... : ...`, 而且两边必然长得不一样。
 *   现在能力集中声明在 RENDERERS 里, UI 只问「当前渲染器支不支持 X」。
 *
 * ★ 契约的边界(务必诚实): 本轮只把**两侧都已具备**的能力纳入会话共享 ——
 *   周期 / 复权 / 主图叠加层 / 视口。指标清单两侧语义不同(ECharts 是自定义
 *   副图 key, klinecharts 是库内指标名), 尚未统一, 用 indicatorCatalog
 *   显式标为 'isolated'。宁可标注未共享, 也不要假装已共享。
 */
import { KLINE_PERIOD_TABS, PERIOD_CAPABILITY, type KLinePeriod } from '@/lib/klinePeriod'

export type ChartRendererId = 'echarts' | 'klinecharts'

/** 主图叠加层: 与会话共享开关的那一组 */
export interface OverlayCapability {
  /** 主图定量结构(EMA25/89 双轨 + 九转) */
  structure: boolean
  /** 缠论笔 / 中枢 / 买卖点 */
  chan: boolean
  /** 筹码分布 */
  chips: boolean
  /** 策略信号标记(signal_* 列)。两侧都实现了, 且共用同一份中文名/配色 */
  signals: boolean
  /** 监控触发标记(alerts.jsonl)。两侧共用 lib/chart-events 的归约 */
  alerts: boolean
  /** 回测买卖点标记(最近一次策略回测落盘)。同上 */
  trades: boolean
}

export interface ChartCapabilities {
  /** 该渲染器支持的周期档位(顺序同 KLINE_PERIOD_TABS) */
  periods: KLinePeriod[]
  /** 是否支持复权切换(分钟档后端无复权口径, 与渲染器无关, 由 period 判定) */
  adjust: boolean
  overlays: OverlayCapability
  /** 手绘线(趋势线/斐波那契等)。目前只有 ECharts 侧有直连 zrender 的实现 */
  drawing: boolean
  /**
   * 视口读写: 能否把「可见 bar 数 + 右侧偏移」读出来并回填。
   * 两侧都实现了(见 chartViewport.ts), 这是切内核不丢缩放的前提。
   */
  viewport: boolean
  /**
   * 能否按日期定位视口(事件时间轴点击后跳过去)。
   * 两侧都用各自的官方 API 实现了: klinecharts 是 scrollToDataIndex,
   * ECharts 是 dataZoom 的 start/end。做不到的一侧时间轴按钮就不显示。
   */
  focus: boolean
  /**
   * 指标清单是否跨内核共享。'isolated' = 两侧各存各的, 切内核不继承。
   * 统一它需要先把两侧指标名对齐, 属于后续项。
   */
  indicatorCatalog: 'shared' | 'isolated'
}

export interface ChartRendererSpec {
  id: ChartRendererId
  label: string
  /** 切换按钮的说明文案 */
  hint: string
  capabilities: ChartCapabilities
}

function periodsFor(id: ChartRendererId): KLinePeriod[] {
  const cap = PERIOD_CAPABILITY[id]
  return KLINE_PERIOD_TABS.filter(t => (t.key === '1m' ? cap.minute1 : true)).map(t => t.key)
}

export const RENDERERS: Record<ChartRendererId, ChartRendererSpec> = {
  echarts: {
    id: 'echarts',
    label: 'ECharts',
    hint: '切到 ECharts 内核 (g)',
    capabilities: {
      periods: periodsFor('echarts'),
      adjust: true,
      overlays: { structure: true, chan: true, chips: false, signals: true, alerts: true, trades: true },
      drawing: true,
      viewport: true,
      focus: true,
      indicatorCatalog: 'isolated',
    },
  },
  klinecharts: {
    id: 'klinecharts',
    label: 'KLinePro',
    hint: '切到 KLineChart 内核 (g)',
    capabilities: {
      periods: periodsFor('klinecharts'),
      adjust: true,
      overlays: { structure: true, chan: true, chips: true, signals: true, alerts: true, trades: true },
      drawing: false,
      viewport: true,
      focus: true,
      indicatorCatalog: 'isolated',
    },
  },
}

export function rendererSpec(id: ChartRendererId): ChartRendererSpec {
  return RENDERERS[id]
}

/** 当前渲染器是否支持某周期档位 */
export function rendererSupportsPeriod(id: ChartRendererId, p: KLinePeriod): boolean {
  return RENDERERS[id].capabilities.periods.includes(p)
}

/**
 * 切到的目标渲染器不支持当前周期时, 退化到哪个档位。
 * 例如 ECharts 不支持 1m, 从 KLinePro 的 1m 切回去就落到 5m。
 */
export function fallbackPeriod(id: ChartRendererId, from: KLinePeriod): KLinePeriod {
  if (rendererSupportsPeriod(id, from)) return from
  const list = RENDERERS[id].capabilities.periods
  const idx = KLINE_PERIOD_TABS.findIndex(t => t.key === from)
  // 往回找最近的可用档位: 1m -> 5m, 若无则退到 day
  for (let i = idx; i < KLINE_PERIOD_TABS.length; i += 1) {
    const k = KLINE_PERIOD_TABS[i].key
    if (list.includes(k)) return k
  }
  return list.includes('day') ? 'day' : list[0]
}
