/**
 * klinecharts 单内核能力矩阵。
 *
 * 双内核时代(echarts + klinecharts)这里是一份「切渲染器」契约, 用 RENDERERS
 * 声明两个内核各自支持什么, UI 再按能力显隐。现在前端已收敛为 klinecharts
 * 单内核, 那份跨内核对比契约(ChartRendererId / rendererSpec / fallbackPeriod /
 * indicatorCatalog)全部废弃 —— 叠加层开关/时间轴等入口直接读下面这一份声明。
 */
import { periodTabsFor, type KLinePeriod } from '@/lib/klinePeriod'

/** 主图叠加层: 终端层按这一组开关显隐 */
export interface OverlayCapability {
  /** 主图定量结构(EMA25/89 双轨 + 九转) */
  structure: boolean
  /** 缠论笔 / 中枢 / 买卖点 */
  chan: boolean
  /** 筹码分布 */
  chips: boolean
  /** 策略信号标记(signal_* 列) */
  signals: boolean
  /** 监控触发标记(alerts.jsonl) */
  alerts: boolean
  /** 回测买卖点标记 */
  trades: boolean
}

export interface ChartCapabilities {
  /** 支持的周期档位(顺序同 KLINE_PERIOD_TABS) */
  periods: KLinePeriod[]
  /** 是否支持复权切换(分钟档后端无复权口径, 由 period 判定) */
  adjust: boolean
  overlays: OverlayCapability
  /** 手绘线(趋势线/斐波那契等)。klinecharts 侧尚未实现 */
  drawing: boolean
  /** 视口读写(可见 bar 数 + 右侧偏移) */
  viewport: boolean
  /** 按日期定位视口(事件时间轴点击跳转) */
  focus: boolean
}

/** 唯一内核 klinecharts 的能力矩阵 */
export const KLINE_CAPABILITIES: ChartCapabilities = {
  periods: periodTabsFor('klinecharts').map(t => t.key),
  adjust: true,
  overlays: { structure: true, chan: true, chips: true, signals: true, alerts: true, trades: true },
  drawing: false,
  viewport: true,
  focus: true,
}
