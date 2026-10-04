import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { fmtPct } from '@/lib/format'

interface DepthLevel {
  price: number | null
  volume: number | null
}

interface DepthData {
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
  bid: DepthLevel[]
  ask: DepthLevel[]
  wb_ratio: number | null
  wb_diff: number | null
  time: string | null
  timestamp: number | null
}

interface Props {
  symbol: string
  /** 轮询间隔(ms)，undefined 表示不轮询 */
  refetchIntervalMs?: number
}

function fmtPrice(v: number | null | undefined): string {
  return v == null ? '—' : v.toFixed(2)
}

function fmtVolume(v: number | null | undefined): string {
  if (v == null) return '—'
  if (v >= 10000) return `${(v / 10000).toFixed(1)}万`
  return v.toFixed(0)
}

function fmtMarketCap(v: number | null | undefined): string {
  if (v == null) return '—'
  return `${v.toFixed(0)}亿`
}

function pctColor(v: number | null | undefined): string {
  if (v == null) return 'text-muted'
  return v > 0 ? 'text-bull' : v < 0 ? 'text-bear' : 'text-muted'
}

export function DepthPanel({ symbol, refetchIntervalMs }: Props) {
  const depth = useQuery<DepthData>({
    queryKey: ['depth', symbol],
    queryFn: () => api.depthGet(symbol),
    enabled: !!symbol,
    refetchInterval: refetchIntervalMs,
    staleTime: 3000,
  })

  const d = depth.data
  const [mounted, setMounted] = useState(false)
  useEffect(() => setMounted(true), [])

  if (!symbol) return null

  // 与 useQuote 同一口径: v5 在「无数据 + 轮询」下会让 isLoading 长期为 true,
  // 取不到盘口(港美股/无后缀代码)时必须落到"不可用"而不是一直转圈。
  const everFailed = !d && (depth.isError || depth.failureCount > 0)

  if ((depth.isLoading && !everFailed) || !mounted) {
    return (
      <div className="flex h-full items-center justify-center text-xs text-muted">
        盘口加载中…
      </div>
    )
  }

  if (depth.isError || everFailed || !d) {
    return (
      <div className="flex h-full items-center justify-center text-xs text-muted">
        盘口数据不可用
      </div>
    )
  }

  const bidLevels = d.bid ?? []
  const askLevels = d.ask ?? []

  return (
    <div className="flex h-full flex-col bg-surface text-xs">
      {/* 标题栏 */}
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <span className="font-medium text-foreground">{d.name}</span>
        <span className="text-muted">{d.symbol}</span>
      </div>

      {/* 最新价 */}
      <div className="flex items-baseline gap-2 border-b border-border px-3 py-2">
        <span className={`text-lg font-semibold ${pctColor(d.change_pct)}`}>
          {fmtPrice(d.last_price)}
        </span>
        <span className={pctColor(d.change_pct)}>
          {d.change_pct == null ? '—' : `${d.change_pct >= 0 ? '+' : ''}${fmtPct(d.change_pct)}`}
        </span>
      </div>

      {/* 委比委差 */}
      <div className="grid grid-cols-2 gap-1 border-b border-border px-3 py-1.5 text-[11px]">
        <div className="flex justify-between">
          <span className="text-muted">委比</span>
          <span className={d.wb_ratio == null ? 'text-muted' : d.wb_ratio >= 0 ? 'text-bull' : 'text-bear'}>
            {d.wb_ratio == null ? '—' : `${d.wb_ratio >= 0 ? '+' : ''}${d.wb_ratio.toFixed(2)}%`}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-muted">委差</span>
          <span className={d.wb_diff == null ? 'text-muted' : d.wb_diff >= 0 ? 'text-bull' : 'text-bear'}>
            {d.wb_diff == null ? '—' : `${d.wb_diff >= 0 ? '+' : ''}${fmtVolume(d.wb_diff)}`}
          </span>
        </div>
      </div>

      {/* 买卖五档 */}
      <div className="flex-1 overflow-y-auto px-3 py-1.5">
        {/* 卖五 → 卖一 */}
        {askLevels.slice().reverse().map((lv, i) => (
          <div key={`ask-${i}`} className="flex justify-between py-0.5">
            <span className="text-bear">
              卖{5 - i} {fmtPrice(lv.price)}
            </span>
            <span className="text-muted">{fmtVolume(lv.volume)}</span>
          </div>
        ))}
        <div className="my-1 border-t border-border/50" />
        {/* 买一 → 买五 */}
        {bidLevels.map((lv, i) => (
          <div key={`bid-${i}`} className="flex justify-between py-0.5">
            <span className="text-bull">
              买{i + 1} {fmtPrice(lv.price)}
            </span>
            <span className="text-muted">{fmtVolume(lv.volume)}</span>
          </div>
        ))}
      </div>

      {/* 关键指标 */}
      <div className="border-t border-border px-3 py-1.5 text-[11px]">
        <div className="grid grid-cols-2 gap-1">
          <div className="flex justify-between">
            <span className="text-muted">今开</span>
            <span>{fmtPrice(d.open)}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-muted">最高</span>
            <span>{fmtPrice(d.high)}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-muted">最低</span>
            <span>{fmtPrice(d.low)}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-muted">换手</span>
            <span>{d.turnover_rate == null ? '—' : `${d.turnover_rate.toFixed(2)}%`}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-muted">量比</span>
            <span>{d.volume_ratio == null ? '—' : d.volume_ratio.toFixed(2)}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-muted">均价</span>
            <span>{fmtPrice(d.avg_price)}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-muted">总市值</span>
            <span>{fmtMarketCap(d.total_market_cap)}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-muted">流通值</span>
            <span>{fmtMarketCap(d.circulating_market_cap)}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-muted">PE(TTM)</span>
            <span>{d.pe == null ? '—' : d.pe.toFixed(2)}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-muted">PB</span>
            <span>{d.pb == null ? '—' : d.pb.toFixed(2)}</span>
          </div>
        </div>
      </div>

      {/* 涨跌停 */}
      <div className="grid grid-cols-2 gap-1 border-t border-border px-3 py-1.5 text-[11px]">
        <div className="flex justify-between">
          <span className="text-muted">涨停</span>
          <span className="text-bull">{fmtPrice(d.limit_up)}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-muted">跌停</span>
          <span className="text-bear">{fmtPrice(d.limit_down)}</span>
        </div>
      </div>
    </div>
  )
}
