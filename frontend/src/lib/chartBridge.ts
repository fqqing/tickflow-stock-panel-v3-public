/**
 * 渲染器 ↔ 终端层 UI 的三个窄通道。
 *
 * ★ 为什么不塞进 chartSession: 会话是「用户此刻看的是哪张图」的**状态**, 这三个
 *   是**一次性指令 / 上报**, 语义不同。硬塞进会话会有两个后果: ① 每条指令都让
 *   整个终端重渲染(视口那套静默通道就是为了躲这个); ② 上报的日期序列每次刷新
 *   都变, 若走会话通知, 分钟档 6 秒一次的轮询会把终端整棵子树反复重渲染。
 *
 * ★ 共同约定: 订阅时**立即把当前值推一次**。这样「先点了时间轴、后切内核」也
 *   不会丢 —— 新渲染器挂载时补收到那条待办指令, 定位照样生效。
 */
import type { TimelineEvent } from '@/lib/chart-timeline'

export interface ChartFocusRequest {
  /** 单调递增: 同一天可重复点击, 靠它区分「又点了一次」和「还是上次那条」 */
  seq: number
  /** YYYY-MM-DD */
  date: string
  /**
   * 目标 bar 落在视口的横向位置 (0=最左, 1=最右)。
   * 默认 0.35: 目标靠左, 右边留出大半屏看它之后发生了什么 —— 复盘要看的是后续。
   */
  anchor: number
}

function makeChannel<T>(empty: T, same: (a: T, b: T) => boolean) {
  let value: T = empty
  const listeners = new Set<(v: T) => void>()
  return {
    get: (): T => value,
    set(next: T): void {
      if (same(value, next)) return
      value = next
      for (const l of Array.from(listeners)) l(value)
    },
    subscribe(l: (v: T) => void): () => void {
      listeners.add(l)
      l(value)
      return () => { listeners.delete(l) }
    },
  }
}

// ── 1. 定位指令: 时间轴点击 → 渲染器把视口挪过去 ──────────────
let focusSeq = 0
let pendingFocus: ChartFocusRequest | null = null
const focusListeners = new Set<(r: ChartFocusRequest) => void>()

export const chartFocus = {
  request: (date: string, anchor = 0.35): void => {
    focusSeq += 1
    const req: ChartFocusRequest = { seq: focusSeq, date, anchor }
    pendingFocus = req
    for (const l of Array.from(focusListeners)) l(req)
  },
  /** 渲染器执行完调用: 静默清空, 不再通知(否则会无限回环) */
  consume: (): void => { pendingFocus = null },
  pending: (): ChartFocusRequest | null => pendingFocus,
  subscribe(l: (r: ChartFocusRequest) => void): () => void {
    focusListeners.add(l)
    // 补发: 挂载前就点过的话, 新渲染器也要执行
    if (pendingFocus) l(pendingFocus)
    return () => { focusListeners.delete(l) }
  },
}

// ── 2. K 线日期序列: 渲染器上报, 时间轴据此把日期换算成横坐标 ──
// 时间轴在终端层, 拿不到渲染器内部的 rows; 反过来让终端层再拉一次日K是重复请求。
// 上报一条 string[] 最省, 且只在「首末日期或长度变了」时才通知(数据刷新不触发)。
const datesSame = (a: string[], b: string[]): boolean =>
  a.length === b.length && a[0] === b[0] && a[a.length - 1] === b[b.length - 1]

export const chartBars = makeChannel<string[]>([], datesSame)

// ── 3. 策略信号事件: 渲染器上报, 时间轴与 K 线上的三角同源 ──
// 只有日线档有 signal_* 列, 关掉开关时上报空数组。
// 指纹取「条数 + 首末的日期与名称」: 全量比对每条信号在数据刷新时会白白通知。
const signalsSame = (a: TimelineEvent[], b: TimelineEvent[]): boolean => {
  if (a.length !== b.length) return false
  if (a.length === 0) return true
  return a[0].date === b[0].date && a[0].label === b[0].label
    && a[a.length - 1].date === b[b.length - 1].date
    && a[a.length - 1].label === b[b.length - 1].label
}

export const chartSignals = makeChannel<TimelineEvent[]>([], signalsSame)
