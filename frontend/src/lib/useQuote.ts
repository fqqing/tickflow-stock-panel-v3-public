/**
 * 个股终端价格单一源。
 *
 * 背景: 此前顶栏/信息条取日 K 最后一根 close, 盘口取 /api/depth 实时快照,
 * 两个源更新频率不同 => 同一屏出现 35.04 与 34.80 两个价格, 且没有快照时间。
 *
 * 做法: 统一走 /api/depth/{symbol}, 并且 queryKey 与 DepthPanel 保持完全一致
 * (['depth', symbol]) —— react-query 会自动去重, 顶栏和盘口共用同一份数据、
 * 同一份轮询, 不可能再出现两个价格。
 */
import { useEffect, useState, type RefObject } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'

export type QuoteSnapshot = Awaited<ReturnType<typeof api.depthGet>>

export interface UnifiedQuote {
  /** 最新价, null = 无实时数据(盘后/港美股/插件不可用) */
  price: number | null
  /** 涨跌额 */
  change: number | null
  /** 涨跌幅(与后端一致的小数或百分数, 展示层用 fmtPct 处理) */
  changePct: number | null
  isUp: boolean
  /** 快照时间字符串, null = 无 */
  snapshotTime: string | null
  /** 是否实时数据 */
  isRealtime: boolean
  isLoading: boolean
  isError: boolean
  /** 原始快照, 供盘口等组件复用完整字段 */
  raw: QuoteSnapshot | undefined
}

const EMPTY: UnifiedQuote = {
  price: null,
  change: null,
  changePct: null,
  isUp: true,
  snapshotTime: null,
  isRealtime: false,
  isLoading: false,
  isError: false,
  raw: undefined,
}

/**
 * 拉取单只标的的实时快照。
 *
 * refetchIntervalMs 传 undefined 表示不轮询(盘后定格), 由调用方按实时行情
 * 运行状态决定, 与 DepthPanel 的用法保持一致。
 */
export function useQuote(
  symbol: string | null | undefined,
  refetchIntervalMs?: number,
): UnifiedQuote {
  const q = useQuery<QuoteSnapshot>({
    queryKey: ['depth', symbol ?? ''],
    queryFn: () => api.depthGet(symbol as string),
    enabled: !!symbol,
    refetchInterval: refetchIntervalMs,
    staleTime: 3000,
    retry: 1,
  })

  if (!symbol) return EMPTY

  const d = q.data
  const price = d?.last_price ?? null
  const prevClose = d?.prev_close ?? null
  const change = price != null && prevClose != null ? price - prevClose : null

  // ★ 「取不到」不能被说成「正在加载」。
  //   react-query v5 在「无数据 + 定期轮询」下会把 query 周期性带回 pending 状态,
  //   于是 isLoading 长期为 true —— 港美股 / 无盘口标的的顶栏就永远显示"载入中…",
  //   后面那句"盘口不可用 · 以图表为准"永远没机会出现(真机实测 12s 持续转圈)。
  //   用 failureCount 记住"失败过", 一旦失败过就不再声称加载中。
  const everFailed = !!d ? false : (q.isError || q.failureCount > 0)

  return {
    price,
    change,
    changePct: d?.change_pct ?? null,
    isUp: (change ?? 0) >= 0,
    snapshotTime: d?.time ?? null,
    isRealtime: price != null,
    isLoading: q.isLoading && !everFailed,
    isError: q.isError || everFailed,
    raw: d,
  }
}

/**
 * 容器可用高度测量 —— 用于让图表高度随窗口自适应, 替代写死的 420/520。
 *
 * StockDailyKChart 的 height 是数字 prop(内部要据此算副图高度),
 * 所以这里返回数字而非 CSS 高度。offset 用于扣除信息条/工具条等固定占用。
 */
export function useElementHeight<T extends HTMLElement>(
  ref: RefObject<T | null>,
  offset = 0,
  min = 240,
): number {
  const [h, setH] = useState(min)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    const measure = () => {
      const next = Math.max(min, el.clientHeight - offset)
      setH(prev => (Math.abs(prev - next) > 4 ? next : prev))
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [ref, offset, min])

  return h
}
