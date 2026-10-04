/**
 * 终端响应式断点（P2）。
 *
 * 三档：
 *   three  ≥1440  左栏 + 图表 + 右栏
 *   two    1024–1440  图表 + 右栏（左栏收为抽屉）
 *   one    <1024   图表全宽（左右都收为抽屉）
 *
 * 用视口宽度判定（终端页本身是主区，误差可接受），SSR 下默认 three。
 */
import { useEffect, useState } from 'react'

export type LayoutMode = 'one' | 'two' | 'three'

const THREE_MIN = 1440
const TWO_MIN = 1024

function measure(): LayoutMode {
  if (typeof window === 'undefined') return 'three'
  const w = window.innerWidth
  if (w >= THREE_MIN) return 'three'
  if (w >= TWO_MIN) return 'two'
  return 'one'
}

export function useLayoutMode(): LayoutMode {
  const [mode, setMode] = useState<LayoutMode>(measure)

  useEffect(() => {
    let raf = 0
    const onResize = () => {
      cancelAnimationFrame(raf)
      raf = requestAnimationFrame(() => {
        setMode(prev => {
          const next = measure()
          return next === prev ? prev : next
        })
      })
    }
    window.addEventListener('resize', onResize)
    return () => {
      cancelAnimationFrame(raf)
      window.removeEventListener('resize', onResize)
    }
  }, [])

  return mode
}
