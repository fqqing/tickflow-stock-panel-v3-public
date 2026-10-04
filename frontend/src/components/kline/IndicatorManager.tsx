/**
 * KLineChart 内核的指标管理器(S2)。
 *
 * 两块内容:
 *   1. 已添加 —— 改参数(number input, 即时生效) / 删除; 同名指标可以加多份(不同参数)。
 *   2. 添加 —— 按主图 / 副图分组列出 klinecharts 全部内置指标。
 *
 * 参数为空数组表示"跟随库默认", 此时显示「默认」而不是 0 个输入框;
 * 组件挂载后 KLinePro 会回读真实 calcParams 写回, 输入框随后出现。
 */
import { useEffect, useMemo } from 'react'
import {
  INDICATOR_METAS,
  makeKey,
  metaOf,
  type IndicatorConfig,
  type IndicatorMeta,
} from '@/lib/klineIndicators'
import { cn } from '@/lib/cn'

export interface IndicatorManagerProps {
  configs: IndicatorConfig[]
  onChange: (next: IndicatorConfig[]) => void
  onClose: () => void
}

function ParamInputs({
  meta,
  params,
  onChange,
}: {
  meta?: IndicatorMeta
  params: number[]
  onChange: (next: number[]) => void
}) {
  if (params.length === 0) {
    return <span className="text-[10px] text-muted/60">默认</span>
  }
  return (
    <div className="flex flex-wrap items-center gap-1">
      {params.map((v, i) => (
        <label key={i} className="flex items-center gap-0.5 text-[10px] text-muted/70">
          {meta?.labels?.[i] ?? `参数${i + 1}`}
          <input
            type="number"
            value={v}
            onChange={e => {
              const raw = e.target.value
              if (raw === '') return
              const n = Number(raw)
              if (!Number.isFinite(n)) return
              const next = params.slice()
              next[i] = n
              onChange(next)
            }}
            className="h-5 w-11 rounded border border-border bg-elevated px-1 text-[10px] text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent"
          />
        </label>
      ))}
    </div>
  )
}

export function IndicatorManager({ configs, onChange, onClose }: IndicatorManagerProps) {
  // 面板开着的期间自己接管 Esc。终端层看到 data-overlay-panel 就不会执行
  // 「Esc = 返回上一页」, 所以这里不接管的话 Esc 会变成无操作。
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.preventDefault(); onClose() }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const counts = useMemo(() => {
    const m = new Map<string, number>()
    for (const c of configs) m.set(c.name, (m.get(c.name) ?? 0) + 1)
    return m
  }, [configs])

  const add = (meta: IndicatorMeta) => {
    const next = configs.concat({ key: makeKey(configs, meta.name), name: meta.name, group: meta.group, params: [] })
    onChange(next)
  }

  const update = (key: string, patch: Partial<IndicatorConfig>) => {
    onChange(configs.map(c => (c.key === key ? { ...c, ...patch } : c)))
  }

  const remove = (key: string) => {
    onChange(configs.filter(c => c.key !== key))
  }

  const groups: { title: string; metas: IndicatorMeta[] }[] = [
    { title: '主图', metas: INDICATOR_METAS.filter(m => m.group === 'main') },
    { title: '副图', metas: INDICATOR_METAS.filter(m => m.group === 'sub') },
  ]

  return (
    <div
      // data-overlay-panel: 告知终端层的全局键盘处理「有浮层面板开着」。
      // 没有这个标记时, 面板开着按 Esc 会命中 StockTerminal 的 `navigate(-1)`,
      // 变成「想关面板却被退回上一只股票」。
      data-overlay-panel="indicator"
      className="absolute right-1 top-1 z-30 w-[268px] overflow-hidden rounded-card border border-border bg-surface shadow-lg"
    >
      <div className="flex items-center justify-between border-b border-border px-2 py-1.5">
        <span className="text-[11px] font-medium text-foreground">指标</span>
        <button
          type="button"
          onClick={onClose}
          title="关闭"
          className="h-5 w-5 rounded text-[12px] leading-none text-muted transition-colors hover:bg-elevated hover:text-foreground"
        >
          ×
        </button>
      </div>

      <div className="max-h-[240px] overflow-y-auto px-2 py-1.5">
        {configs.length === 0 && (
          <div className="py-2 text-center text-[10px] text-muted/60">未添加任何指标</div>
        )}
        {configs.map(c => {
          const meta = metaOf(c.name)
          return (
            <div key={c.key} className="border-b border-border/60 py-1.5 last:border-b-0">
              <div className="flex items-center gap-1">
                <span className="text-[11px] text-foreground">{meta?.cn ?? c.name}</span>
                <span className="font-mono text-[10px] text-muted/60">{c.name}</span>
                <span
                  className={cn(
                    'rounded px-1 text-[9px]',
                    c.group === 'main' ? 'bg-accent/15 text-accent' : 'bg-elevated text-muted',
                  )}
                >
                  {c.group === 'main' ? '主图' : '副图'}
                </span>
                <button
                  type="button"
                  onClick={() => remove(c.key)}
                  title="删除"
                  className="ml-auto h-5 w-5 rounded text-[12px] leading-none text-muted transition-colors hover:bg-danger/15 hover:text-danger"
                >
                  ×
                </button>
              </div>
              <div className="pt-1">
                <ParamInputs meta={meta} params={c.params} onChange={params => update(c.key, { params })} />
              </div>
            </div>
          )
        })}
      </div>

      <div className="border-t border-border px-2 py-1.5">
        {groups.map(g => (
          <div key={g.title} className="pb-1 last:pb-0">
            <div className="pb-1 text-[10px] text-muted/70">添加{g.title}</div>
            <div className="flex flex-wrap gap-1">
              {g.metas.map(m => {
                const n = counts.get(m.name) ?? 0
                return (
                  <button
                    key={m.name}
                    type="button"
                    onClick={() => add(m)}
                    title={m.name}
                    className={cn(
                      'h-5 rounded border px-1.5 text-[10px] transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent',
                      n > 0
                        ? 'border-accent/30 bg-accent/10 text-accent'
                        : 'border-border text-muted hover:bg-elevated hover:text-foreground',
                    )}
                  >
                    {m.cn}
                    {n > 1 ? ` ×${n}` : ''}
                  </button>
                )
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
