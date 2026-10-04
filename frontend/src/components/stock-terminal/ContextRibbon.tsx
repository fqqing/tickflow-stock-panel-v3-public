/**
 * 触发上下文条 —— 回答「我为什么打开这只股票」。
 *
 * 商业终端(同花顺/通达信/TradingView)只告诉你现在多少钱, 不告诉你为什么来这里。
 * tickflow 从监控触发、异动扫描、选股结果跳进个股时, 来源/时间/触发价位/信号
 * 标签其实都已经有了, 之前只渲染成弹窗里一条 10px 小字。这里把它升级为独立
 * 横幅, 并提供「回到来源」, 让入口与个股之间可以双向跳转。
 */
import { Activity, Bell, Radar, Star, ArrowUpRight } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { cn } from '@/lib/cn'
import { fmtPct } from '@/lib/format'

export type TriggerKind = 'monitor' | 'abnormal' | 'screener' | 'watchlist'

export interface TriggerContext {
  kind: TriggerKind
  /** 来源名称, 如「放量突破 · 30分钟」 */
  label: string
  /** 触发时间(毫秒时间戳或可解析字符串) */
  ts?: number | string | null
  /** 触发时价格 */
  price?: number | null
  /** 触发时涨跌幅(小数) */
  changePct?: number | null
  message?: string
  signals?: string[]
  /** 返回路径, 如 /monitor */
  backTo?: string
}

const KIND_META: Record<TriggerKind, { icon: typeof Bell; bar: string; chip: string; name: string }> = {
  monitor: {
    icon: Bell,
    bar: 'border-amber-400/30 bg-amber-400/[0.07]',
    chip: 'bg-amber-400/15 text-amber-300',
    name: '监控触发',
  },
  abnormal: {
    icon: Activity,
    bar: 'border-bull/30 bg-bull/[0.07]',
    chip: 'bg-bull/15 text-bull',
    name: '异动',
  },
  screener: {
    icon: Radar,
    bar: 'border-accent/30 bg-accent/[0.07]',
    chip: 'bg-accent/15 text-accent',
    name: '选股',
  },
  watchlist: {
    icon: Star,
    bar: 'border-border bg-elevated/60',
    chip: 'bg-elevated text-secondary',
    name: '自选',
  },
}

function fmtTs(ts: number | string | null | undefined): string {
  if (ts == null) return ''
  const d = typeof ts === 'number' ? new Date(ts) : new Date(ts)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  })
}

interface Props {
  ctx: TriggerContext | null
  /** 在图表中定位到该价位(由页面实现, 传入则显示「定位」按钮) */
  onLocatePrice?: (price: number) => void
}

export function ContextRibbon({ ctx, onLocatePrice }: Props) {
  const navigate = useNavigate()
  if (!ctx) return null

  const meta = KIND_META[ctx.kind]
  const Icon = meta.icon
  const ts = fmtTs(ctx.ts)

  return (
    <div className={cn('flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-1.5', meta.bar)}>
      <span className="flex shrink-0 items-center gap-1.5">
        <Icon className="h-3.5 w-3.5" />
        <span className={cn('rounded px-1.5 py-0.5 text-[10px] font-medium', meta.chip)}>
          {meta.name}
        </span>
        <span className="max-w-[220px] truncate text-xs text-foreground/85" title={ctx.label}>
          {ctx.label}
        </span>
      </span>

      {ts && <span className="shrink-0 font-mono text-[11px] text-secondary">{ts}</span>}

      {ctx.price != null && (
        <span className="flex shrink-0 items-center gap-1">
          <span className="font-mono text-[11px] text-foreground/80">{ctx.price.toFixed(2)}</span>
          {ctx.changePct != null && (
            <span className={cn('font-mono text-[11px]', ctx.changePct >= 0 ? 'text-bull' : 'text-bear')}>
              {fmtPct(ctx.changePct)}
            </span>
          )}
          {onLocatePrice && (
            <button
              type="button"
              onClick={() => onLocatePrice(ctx.price as number)}
              className="rounded px-1 text-[10px] text-accent transition-colors hover:bg-accent/10 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent"
              title="在图表上定位该价位"
            >
              定位
            </button>
          )}
        </span>
      )}

      {ctx.message && (
        <span className="min-w-0 truncate text-[11px] text-secondary" title={ctx.message}>
          {ctx.message}
        </span>
      )}

      {(ctx.signals ?? []).length > 0 && (
        <span className="flex flex-wrap items-center gap-1">
          {ctx.signals!.map((s, i) => (
            <span key={i} className="rounded bg-accent/10 px-1.5 py-0.5 text-[10px] text-accent/85">
              {s}
            </span>
          ))}
        </span>
      )}

      {ctx.backTo && (
        <button
          type="button"
          onClick={() => navigate(ctx.backTo as string)}
          className="ml-auto flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-secondary transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent"
          title="回到触发来源"
        >
          回到来源
          <ArrowUpRight className="h-3 w-3" />
        </button>
      )}
    </div>
  )
}
