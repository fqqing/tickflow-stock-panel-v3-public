/**
 * 缠论买卖点/当前状态的单元格渲染（自选页与策略页共享）。
 *
 * 从 ScreenerTable 抽出来，因为两个页面的表格骨架都是 StockDataTable，
 * 差异只在 renderCell —— 缠论单元格没有页面特有交互，抽成共享原语可让
 * 「自选里的票也能直接看缠论」，且判断口径与选股页永远一致。
 *
 * 调色板走 CSS 变量（--bull 红涨 / --bear 绿跌），自动跟随主题，无需页面感知。
 */
import type { ReactNode } from 'react'
import type { ChanAnnotation } from '@/lib/api'
import { fmtPrice } from '@/lib/format'

/** 买卖点标签配色：买点取 bull 红涨, 卖点取 bear 绿跌 (A 股惯例)。 */
const TAG_CLS: Record<string, string> = {
  '1buy': 'bg-bull/10 text-bull border-bull/20',
  '2buy': 'bg-bull/15 text-bull border-bull/30',
  '3buy': 'bg-bull/25 text-bull border-bull/45',
  '1sell': 'bg-bear/10 text-bear border-bear/20',
  '2sell': 'bg-bear/15 text-bear border-bear/30',
  '3sell': 'bg-bear/25 text-bear border-bear/45',
}
const TAG_FALLBACK_CLS = 'bg-elevated text-secondary border-border'
/** 失效标记: 买点被跌破 / 卖点被新高升破后, 标签置灰加删除线 (结构已作废) */
const TAG_INVALID_CLS = 'bg-elevated text-muted border-border opacity-60 line-through'
/** 「当前状态」列的无信号/占位单元格文案 */
const PLACEHOLDER_CLS = 'text-[11px] text-muted'

/** 买卖单侧标注单元格（缠论买点 / 缠论卖点列）。 */
export function ChanSideCell({
  ann,
  isSell,
  loading,
  colId,
}: {
  ann?: ChanAnnotation
  isSell: boolean
  loading?: boolean
  colId: string
}): ReactNode {
  const side = isSell
    ? {
        kind: ann?.sell_kind ?? null,
        label: ann?.sell_label,
        price: ann?.sell_price ?? null,
        barsSince: ann?.sell_bars_since ?? null,
        text: ann?.sell_text,
        invalid: ann?.sell_invalid ?? false,
      }
    : {
        kind: ann?.kind ?? null,
        label: ann?.label,
        price: ann?.price ?? null,
        barsSince: ann?.bars_since ?? null,
        text: ann?.text,
        invalid: ann?.invalid ?? false,
      }
  // 键缺失 = 还没算到；kind=null = 算了但近期无信号
  if (!ann) {
    return (
      <td key={colId} className="px-3 py-2">
        <span className={`${PLACEHOLDER_CLS} ${loading ? 'animate-pulse' : ''}`}>
          {loading ? '计算中…' : '—'}
        </span>
      </td>
    )
  }
  if (!side.kind) {
    return (
      <td key={colId} className="px-3 py-2">
        <span className={PLACEHOLDER_CLS} title={side.text}>—</span>
      </td>
    )
  }
  return (
    <td key={colId} className="px-3 py-2">
      <div className="flex items-center gap-1.5" title={side.invalid ? `${side.text ?? ''} (已失效)` : side.text}>
        <span className={`inline-block shrink-0 px-1.5 py-px rounded text-[10px] font-semibold leading-tight border ${side.invalid ? TAG_INVALID_CLS : (TAG_CLS[side.kind] ?? TAG_FALLBACK_CLS)}`}>
          {side.label}
        </span>
        {side.price != null && (
          <span className="text-[11px] text-secondary num tabular-nums">{fmtPrice(side.price)}</span>
        )}
        {side.barsSince != null && (
          <span className="shrink-0 text-[10px] text-muted num tabular-nums">{side.barsSince}根前</span>
        )}
      </div>
    </td>
  )
}

/** 「当前状态」单元格：后端已合成 (更近一侧主导 + 失效检查 + 买点距离)。 */
export function ChanStateCell({
  ann,
  loading,
  colId,
}: {
  ann?: ChanAnnotation
  loading?: boolean
  colId: string
}): ReactNode {
  if (!ann) {
    return (
      <td key={colId} className="px-3 py-2">
        <span className={`${PLACEHOLDER_CLS} ${loading ? 'animate-pulse' : ''}`}>
          {loading ? '计算中…' : '—'}
        </span>
      </td>
    )
  }
  if (!ann.state_side || !ann.state_label) {
    return (
      <td key={colId} className="px-3 py-2">
        <span className={PLACEHOLDER_CLS} title={ann.state_text}>—</span>
      </td>
    )
  }
  const stateCls = ann.state_invalid
    ? TAG_INVALID_CLS
    : ann.state_side === 'buy'
      ? 'bg-bull/15 text-bull border-bull/30'
      : 'bg-bear/15 text-bear border-bear/30'
  return (
    <td key={colId} className="px-3 py-2">
      <div className="flex items-center gap-1.5" title={ann.state_text}>
        <span className={`inline-block shrink-0 px-1.5 py-px rounded text-[10px] font-semibold leading-tight border ${stateCls}`}>
          {ann.state_label}
        </span>
      </div>
    </td>
  )
}
