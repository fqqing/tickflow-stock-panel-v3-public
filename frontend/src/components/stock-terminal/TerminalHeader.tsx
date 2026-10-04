/**
 * 个股终端顶栏 —— 只承载三件事: 我是谁 / 现在多少钱 / 这个价是什么时间的。
 *
 * 与旧弹窗顶栏的区别: 旧顶栏把 4 个区间预设 + 2 个日期选择器 + 视图切换 +
 * 5 个图标按钮全塞在一行, 控件数量比图表还抢眼。这里把区间类控件下沉到图表
 * 上方的 RangeBar, 顶栏只留标识 + 唯一价格 + 少量动作。
 *
 * 价格来自 useQuote(与盘口共用 ['depth', symbol] 缓存), 全屏只有一个价格。
 */
import { useEffect, useRef, useState } from 'react'
import { ArrowLeft, Star, RadioTower, RefreshCw, MoreHorizontal, Copy, Check } from 'lucide-react'
import { cn } from '@/lib/cn'
import { fmtPct } from '@/lib/format'
import type { UnifiedQuote } from '@/lib/useQuote'

/** 市场后缀徽章: 自己实现, 避免依赖 stock-table/primitives 的返回类型差异 */
function MarketBadge({ symbol }: { symbol: string }) {
  const suffix = symbol.includes('.') ? symbol.split('.').pop()!.toUpperCase() : ''
  if (!suffix) return null
  const cls =
    suffix === 'SH' ? 'border-rose-400/40 text-rose-300'
      : suffix === 'SZ' ? 'border-sky-400/40 text-sky-300'
        : suffix === 'HK' ? 'border-amber-400/40 text-amber-300'
          : 'border-violet-400/40 text-violet-300'
  return (
    <span className={cn('inline-flex h-[18px] items-center rounded border px-1 text-[10px] font-semibold leading-none', cls)}>
      {suffix}
    </span>
  )
}

interface Props {
  symbol: string
  name?: string
  quote: UnifiedQuote
  inWatchlist?: boolean
  onToggleWatchlist?: () => void
  watchlistPending?: boolean
  onMonitor?: () => void
  onRefresh?: () => void
  onClose?: () => void
  /** 额外菜单项(由页面注入, 如「在弹窗中打开」) */
  extraMenuItems?: { label: string; onClick: () => void }[]
}

export function TerminalHeader({
  symbol,
  name,
  quote,
  inWatchlist,
  onToggleWatchlist,
  watchlistPending,
  onMonitor,
  onRefresh,
  onClose,
  extraMenuItems,
}: Props) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [copied, setCopied] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!menuOpen) return
    const onDoc = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [menuOpen])

  const up = quote.isUp
  const clr = quote.price == null ? 'text-muted' : up ? 'text-bull' : 'text-bear'

  const handleCopy = () => {
    navigator.clipboard?.writeText(symbol).catch(() => {})
    setCopied(true)
    setTimeout(() => setCopied(false), 1200)
  }

  return (
    <header className="flex h-14 shrink-0 items-center gap-4 border-b border-border px-4">
      {/* 左: 返回 + 标识 */}
      <div className="flex min-w-0 items-center gap-2">
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            className="rounded-btn p-1.5 text-secondary transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent"
            aria-label="返回上一页"
            title="返回"
          >
            <ArrowLeft className="h-4 w-4" />
          </button>
        )}
        <MarketBadge symbol={symbol} />
        <span className="shrink-0 font-mono text-sm font-medium text-foreground">{symbol}</span>
        {name && <span className="truncate text-xs text-secondary">{name}</span>}
      </div>

      {/* 中: 全屏唯一的价格 */}
      <div className="flex min-w-0 items-baseline gap-2">
        <span className={cn('font-mono text-2xl font-semibold tabular-nums', clr)}>
          {quote.price == null ? '—' : quote.price.toFixed(2)}
        </span>
        {quote.change != null && (
          <span className={cn('font-mono text-xs tabular-nums', clr)}>
            {up ? '+' : ''}{quote.change.toFixed(2)}
          </span>
        )}
        {quote.changePct != null && (
          <span className={cn('font-mono text-xs tabular-nums', clr)}>
            {fmtPct(quote.changePct)}
          </span>
        )}
        <span className="shrink-0 font-mono text-[11px] text-muted">
          {quote.isRealtime
            ? `快照 ${quote.snapshotTime ?? '—'}`
            // ★ isError 必须排在 isLoading 前: 取不到盘口(港美股 / 无盘口标的)时
            //   react-query 在「无数据 + 轮询重取」下 isPending 会一直是 true,
            //   于是 isLoading 恒为 true, 这条兜底文案永远显示不出来, 用户看到的
            //   是「载入中…」无限转 —— 但实际上后端已经明确回复取不到了。
            : quote.isError ? '盘口不可用 · 以图表为准'
            : quote.isLoading ? '载入中…' : '盘口不可用 · 以图表为准'}
        </span>
      </div>

      {/* 右: 动作 */}
      <div className="ml-auto flex shrink-0 items-center gap-1">
        {onToggleWatchlist && (
          <button
            type="button"
            onClick={onToggleWatchlist}
            disabled={watchlistPending}
            className={cn(
              'rounded-btn p-1.5 transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent disabled:opacity-50',
              inWatchlist ? 'text-[#FACC15]' : 'text-secondary hover:bg-elevated hover:text-foreground',
            )}
            title={inWatchlist ? '移出自选' : '加入自选'}
            aria-label={inWatchlist ? `将 ${symbol} 移出自选` : `将 ${symbol} 加入自选`}
          >
            <Star className={cn('h-4 w-4', inWatchlist && 'fill-current')} />
          </button>
        )}
        {onMonitor && (
          <button
            type="button"
            onClick={onMonitor}
            className="rounded-btn p-1.5 text-amber-400 transition-colors hover:bg-amber-400/10 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent"
            title="加监控"
            aria-label="加监控"
          >
            <RadioTower className="h-4 w-4" />
          </button>
        )}
        {onRefresh && (
          <button
            type="button"
            onClick={onRefresh}
            className="rounded-btn p-1.5 text-secondary transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent"
            title="刷新"
            aria-label="刷新"
          >
            <RefreshCw className="h-4 w-4" />
          </button>
        )}

        {/* 溢出菜单: 次要动作 */}
        <div className="relative" ref={menuRef}>
          <button
            type="button"
            onClick={() => setMenuOpen(v => !v)}
            className="rounded-btn p-1.5 text-secondary transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent"
            title="更多"
            aria-label="更多操作"
            aria-expanded={menuOpen}
          >
            <MoreHorizontal className="h-4 w-4" />
          </button>
          {menuOpen && (
            <div className="absolute right-0 top-full z-50 mt-1 w-44 overflow-hidden rounded-btn border border-border bg-surface py-1 shadow-xl">
              <button
                type="button"
                onClick={() => { handleCopy(); setMenuOpen(false) }}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground"
              >
                {copied ? <Check className="h-3.5 w-3.5 text-bear" /> : <Copy className="h-3.5 w-3.5" />}
                复制代码
              </button>
              {(extraMenuItems ?? []).map(item => (
                <button
                  key={item.label}
                  type="button"
                  onClick={() => { item.onClick(); setMenuOpen(false) }}
                  className="block w-full px-3 py-1.5 text-left text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground"
                >
                  {item.label}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </header>
  )
}
