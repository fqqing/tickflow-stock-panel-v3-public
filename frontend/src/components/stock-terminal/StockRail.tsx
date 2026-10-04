/**
 * 终端左栏「股票轨道」（P2）：自选 + 最近查看，点击即换股。
 * 纯展示组件，数据由 StockTerminal 注入。
 */
import { useMemo } from 'react'
import { cn } from '@/lib/cn'

export interface RailItem { symbol: string; name?: string | null }

interface Props {
  watchlist: RailItem[]
  recent: RailItem[]
  current: string
  onSelect: (symbol: string) => void
  className?: string
}

function code(symbol: string): string {
  return symbol.split('.')[0] || symbol
}

export function StockRail({ watchlist, recent, current, onSelect, className }: Props) {
  const items = useMemo(() => {
    const seen = new Set<string>()
    const out: { item: RailItem; group: string }[] = []
    for (const it of watchlist) {
      if (seen.has(it.symbol)) continue
      seen.add(it.symbol)
      out.push({ item: it, group: '自选' })
    }
    for (const it of recent) {
      if (seen.has(it.symbol)) continue
      seen.add(it.symbol)
      out.push({ item: it, group: '最近' })
    }
    return out
  }, [watchlist, recent])

  let lastGroup = ''

  return (
    <nav className={cn('flex h-full min-h-0 flex-col overflow-hidden rounded-card border border-border bg-surface', className)}>
      <div className="shrink-0 border-b border-border px-2 py-1.5 text-[11px] font-mono text-muted">
        股票
      </div>
      <ul className="min-h-0 flex-1 overflow-y-auto py-1">
        {items.length === 0 && (
          <li className="px-2 py-2 text-[11px] text-muted/70">暂无自选 / 最近查看</li>
        )}
        {items.map(({ item, group }) => {
          const head = group !== lastGroup ? group : null
          lastGroup = group
          const active = item.symbol === current
          return (
            <li key={item.symbol}>
              {head && (
                <div className="px-2 pb-0.5 pt-1.5 text-[10px] font-mono text-muted/50">{head}</div>
              )}
              <button
                type="button"
                onClick={() => onSelect(item.symbol)}
                title={item.symbol}
                className={cn(
                  'flex w-full items-baseline gap-1.5 px-2 py-1 text-left transition-colors',
                  active ? 'bg-accent/15 text-accent' : 'text-secondary hover:bg-elevated',
                )}
              >
                <span className="font-mono text-[11px] tabular-nums">{code(item.symbol)}</span>
                <span className="truncate text-[11px] text-muted">{item.name ?? ''}</span>
              </button>
            </li>
          )
        })}
      </ul>
    </nav>
  )
}
