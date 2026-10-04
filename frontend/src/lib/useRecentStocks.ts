/**
 * 终端「最近查看」列表（P2，纯前端 localStorage）。
 * 用于左栏股票轨道与 [ / ] 快速换股。
 */
import { useCallback, useEffect, useState } from 'react'

export interface RecentStock { symbol: string; name?: string }

const KEY = 'tickflow.terminal.recent'
const MAX = 30

function load(): RecentStock[] {
  try {
    const raw = localStorage.getItem(KEY)
    const arr = raw ? JSON.parse(raw) : []
    return Array.isArray(arr) ? arr.filter((r: RecentStock) => typeof r?.symbol === 'string').slice(0, MAX) : []
  } catch {
    return []
  }
}

function save(list: RecentStock[]) {
  try {
    localStorage.setItem(KEY, JSON.stringify(list.slice(0, MAX)))
  } catch {
    // ignore
  }
}

export function useRecentStocks(current?: { symbol: string; name?: string }) {
  const [list, setList] = useState<RecentStock[]>(load)

  useEffect(() => {
    if (!current?.symbol) return
    setList(prev => {
      const next = [current, ...prev.filter(r => r.symbol !== current.symbol)].slice(0, MAX)
      save(next)
      return next
    })
  }, [current?.symbol, current?.name])

  const clear = useCallback(() => {
    setList([])
    save([])
  }, [])

  return { recent: list, clearRecent: clear }
}
