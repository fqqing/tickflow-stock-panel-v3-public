/**
 * 终端命令面板（P2，键盘优先的核心）：Ctrl/Cmd+K 或 / 唤起，输入即搜股，回车换股。
 *
 * 键盘：↑↓ 选择、Enter 跳转、Esc 关闭。空输入时列「最近查看」。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import type { RecentStock } from '@/lib/useRecentStocks'

interface Props {
  current: string
  recent: RecentStock[]
  onPick: (symbol: string) => void
  onClose: () => void
}

export function CommandPalette({ current, recent, onPick, onClose }: Props) {
  const [q, setQ] = useState('')
  const [cursor, setCursor] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLUListElement>(null)

  const search = useQuery({
    queryKey: QK.instrumentSearch(q),
    queryFn: () => api.instrumentSearch(q, 20),
    enabled: q.trim().length > 0,
    placeholderData: prev => prev,
  })

  const results = useMemo<RecentStock[]>(() => {
    const term = q.trim()
    if (!term) return recent.filter(r => r.symbol !== current)
    const hits = (search.data?.results ?? []).map(r => ({ symbol: r.symbol, name: r.name }))
    return hits
  }, [q, search.data?.results, recent, current])

  useEffect(() => { setCursor(0) }, [q])

  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(`[data-idx="${cursor}"]`)
    el?.scrollIntoView({ block: 'nearest' })
  }, [cursor])

  useEffect(() => { inputRef.current?.focus() }, [])

  const commit = (idx: number) => {
    const hit = results[idx]
    if (hit) onPick(hit.symbol)
    else onClose()
  }

  return (
    <div
      className="absolute inset-0 z-30 flex items-start justify-center bg-black/50 p-4"
      onClick={onClose}
    >
      <div
        className="mt-16 w-full max-w-lg overflow-hidden rounded-card border border-border bg-surface shadow-xl"
        onClick={e => e.stopPropagation()}
      >
        <input
          ref={inputRef}
          value={q}
          onChange={e => setQ(e.target.value)}
          placeholder="搜索代码 / 名称，回车换股"
          className="w-full border-b border-border bg-transparent px-3 py-2.5 text-sm text-foreground outline-none placeholder:text-muted/60"
          onKeyDown={e => {
            if (e.key === 'ArrowDown') { e.preventDefault(); setCursor(c => Math.min(c + 1, results.length - 1)) }
            else if (e.key === 'ArrowUp') { e.preventDefault(); setCursor(c => Math.max(c - 1, 0)) }
            else if (e.key === 'Enter') { e.preventDefault(); commit(cursor) }
            else if (e.key === 'Escape') { e.preventDefault(); onClose() }
          }}
        />
        <ul ref={listRef} className="max-h-72 overflow-y-auto py-1">
          {results.length === 0 && (
            <li className="px-3 py-2 text-xs text-muted">
              {q.trim() ? (search.isLoading ? '搜索中…' : '无匹配标的') : '暂无最近查看'}
            </li>
          )}
          {results.map((r, i) => (
            <li key={r.symbol}>
              <button
                type="button"
                data-idx={i}
                onMouseEnter={() => setCursor(i)}
                onClick={() => commit(i)}
                className={cn(
                  'flex w-full items-baseline gap-2 px-3 py-1.5 text-left text-sm transition-colors',
                  i === cursor ? 'bg-accent/15 text-accent' : 'text-secondary hover:bg-elevated',
                )}
              >
                <span className="font-mono text-xs tabular-nums">{r.symbol}</span>
                <span className="truncate text-xs text-muted">{r.name ?? ''}</span>
              </button>
            </li>
          ))}
        </ul>
        <div className="flex items-center gap-3 border-t border-border px-3 py-1.5 text-[10px] text-muted/70">
          <span>↑↓ 选择</span><span>Enter 打开</span><span>Esc 关闭</span>
        </div>
      </div>
    </div>
  )
}
