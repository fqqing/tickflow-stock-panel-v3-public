/** 终端快捷键说明（P2）。按 ? 唤起。 */
import { useEffect } from 'react'

const GROUPS: { title: string; items: [string, string][] }[] = [
  {
    title: '导航',
    items: [
      ['Ctrl/⌘ + K  或  /', '搜索并换股'],
      ['[   /   ]', '上一只 / 下一只（自选 + 最近）'],
      ['Esc', '关闭浮层 / 返回上一页'],
    ],
  },
  {
    title: '视图',
    items: [
      ['r', '折叠 / 展开左栏股票轨道'],
      ['p', '折叠 / 展开右侧盘口'],
      ['c', '缠论笔 / 中枢 / 买卖点开关'],
      ['s', '主图定量结构 (EMA25/89 + 九转)'],
      ['x', '策略信号标记 (仅日线档)'],
      ['a', '监控触发标记 (仅日线档)'],
      ['t', '回测买卖点标记 (仅日线档)'],
      ['e', '事件时间轴 (点条上的竖线, 视口跳到那天)'],
    ],
  },
  {
    title: '周期（KLinePro 内核）',
    items: [
      ['1', '日 K'],
      ['2', '周 K'],
      ['3', '月 K'],
    ],
  },
  {
    title: '其他',
    items: [
      ['?', '打开本帮助'],
      ['g', '切换图表内核（ECharts / KLinePro）'],
    ],
  },
]

export function ShortcutHelp({ onClose }: { onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.preventDefault(); onClose() }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="absolute inset-0 z-30 flex items-center justify-center bg-black/50 p-4" onClick={onClose}>
      <div
        className="w-full max-w-md overflow-hidden rounded-card border border-border bg-surface shadow-xl"
        onClick={e => e.stopPropagation()}
      >
        <div className="border-b border-border px-4 py-2.5 text-sm font-medium">键盘快捷键</div>
        <div className="max-h-[70vh] overflow-y-auto px-4 py-3">
          {GROUPS.map(g => (
            <div key={g.title} className="mb-3 last:mb-0">
              <div className="mb-1 text-[11px] font-mono text-muted/70">{g.title}</div>
              <dl className="space-y-0.5">
                {g.items.map(([k, v]) => (
                  <div key={k} className="flex items-baseline justify-between gap-4">
                    <dt className="font-mono text-[11px] text-accent">{k}</dt>
                    <dd className="text-xs text-secondary">{v}</dd>
                  </div>
                ))}
              </dl>
            </div>
          ))}
        </div>
        <div className="border-t border-border px-4 py-2 text-[10px] text-muted/70">按 Esc 关闭</div>
      </div>
    </div>
  )
}
