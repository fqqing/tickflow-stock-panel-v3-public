import { useState, useCallback, useEffect, useRef } from 'react'
import { storage } from '@/lib/storage'
import { api } from '@/lib/api'

/**
 * 策略池 — 服务端存储(按用户隔离), localStorage 仅作初始快照兜底。
 *
 * 迁移逻辑: 首次挂载时拉取服务端 strategy_pool;
 *   - 服务端有值 → 采用服务端并回写 localStorage;
 *   - 服务端为空且 localStorage 有旧值 → 把旧值推送到服务端(一次性迁移)。
 */
export function useStrategyPool() {
  const [pool, setPool] = useState<string[]>(() => storage.strategyPool.get([]))
  // 拉取完成前不把本地初始值写回服务端(避免用旧值覆盖服务端已有数据)
  const loadedRef = useRef(false)

  // 挂载时从服务端拉取(或迁移本地旧值)
  useEffect(() => {
    let cancelled = false
    api.getStrategyPool()
      .then(res => {
        if (cancelled) return
        const server = res?.strategy_pool ?? []
        const local = storage.strategyPool.get([])
        if (server.length > 0) {
          setPool(server)
          storage.strategyPool.set(server)
        } else if (local.length > 0) {
          // 服务端空 + 本地有旧值 → 一次性迁移到服务端
          api.saveStrategyPool(local).catch(() => {})
        }
        loadedRef.current = true
      })
      .catch(() => { loadedRef.current = true })
    return () => { cancelled = true }
  }, [])

  // 变化时同步写 localStorage(兜底) + 服务端
  useEffect(() => {
    storage.strategyPool.set(pool)
    if (loadedRef.current) {
      api.saveStrategyPool(pool).catch(() => {})
    }
  }, [pool])

  const addToPool = useCallback((id: string) => {
    setPool(prev => prev.includes(id) ? prev : [...prev, id])
  }, [])

  const removeFromPool = useCallback((id: string) => {
    setPool(prev => prev.filter(x => x !== id))
  }, [])

  const reorderPool = useCallback((newOrder: string[]) => {
    setPool(newOrder)
  }, [])

  // 清除池中不存在于 validIds 的失效策略(如本地开发残留的自定义策略)。
  const prune = useCallback((validIds: Iterable<string>) => {
    const validSet = validIds instanceof Set ? validIds : new Set(validIds)
    setPool(prev => {
      if (prev.length === 0) return prev
      const next = prev.filter(id => validSet.has(id))
      return next.length === prev.length ? prev : next
    })
  }, [])

  const isInPool = useCallback((id: string) => pool.includes(id), [pool])

  return { pool, addToPool, removeFromPool, reorderPool, prune, isInPool }
}
