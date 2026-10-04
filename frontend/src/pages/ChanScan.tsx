/**
 * 缠论买卖点全市场扫描。
 *
 * 与策略页的区别：策略页回答「哪些票符合我的选股条件」，这里回答
 * 「此刻全市场哪些票刚出现一买/二买/三买（或一卖/二卖/三卖）」—— 用来找当下的
 * 结构性机会，不依赖策略池。买卖两侧只是方向不同，共用同一套结构算法与扫描路径。
 *
 * 后端一次扫描要遍历全市场日K（约 5500 只），冷启动 20 秒左右，热态（结果缓存命中）
 * 瞬时返回，所以页面用「点按钮才发请求」而不是进入即扫。
 */
import { useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { ScanSearch, RefreshCw, Info, ExternalLink } from 'lucide-react'
import { api, type ChanScanItem } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { fmtPrice } from '@/lib/format'
import { useMarket } from '@/lib/market'

/** 可扫描的买点类型（红系 = 买，与选股结果列的缠论买点配色一致） */
const BUY_KIND_OPTIONS: { key: string; label: string; cls: string }[] = [
  { key: '1buy', label: '一买', cls: 'bg-red-500/10 text-red-400/90 border-red-500/20' },
  { key: '2buy', label: '二买', cls: 'bg-red-500/15 text-red-400 border-red-500/30' },
  { key: '3buy', label: '三买', cls: 'bg-red-500/25 text-red-500 border-red-500/45' },
]

/** 可扫描的卖点类型（绿系 = 卖。卖点同样是结构结论，只是离场方向） */
const SELL_KIND_OPTIONS: { key: string; label: string; cls: string }[] = [
  { key: '1sell', label: '一卖', cls: 'bg-emerald-500/10 text-emerald-500/90 border-emerald-500/20' },
  { key: '2sell', label: '二卖', cls: 'bg-emerald-500/15 text-emerald-500 border-emerald-500/30' },
  { key: '3sell', label: '三卖', cls: 'bg-emerald-500/25 text-emerald-600 border-emerald-500/45' },
]

const KIND_OPTIONS = [...BUY_KIND_OPTIONS, ...SELL_KIND_OPTIONS]
const KIND_CLS: Record<string, string> = Object.fromEntries(KIND_OPTIONS.map(o => [o.key, o.cls]))
const KIND_FALLBACK_CLS = 'bg-elevated text-secondary border-border'

const TREND_META: Record<string, { label: string; cls: string }> = {
  up: { label: '上涨', cls: 'text-red-400' },
  down: { label: '下跌', cls: 'text-emerald-500' },
  range: { label: '盘整', cls: 'text-muted' },
}

/** 买点新鲜度：距今多少根 K 线内算「当下」 */
const FRESHNESS_OPTIONS = [5, 10, 15, 30, 60]
const LOOKBACK = 320
const LIMIT = 300

type ScanParams = { kinds: string[]; recentBars: number; strict: boolean }

/**
 * 扫描结果表。独立导出便于单测/SSR 渲染验证（页面本身要求先点「开始扫描」才有数据）。
 */
export function ChanScanResultTable({
  items,
  onPreview,
}: {
  items: ChanScanItem[]
  onPreview: (item: ChanScanItem) => void
}) {
  return (
    <div className="overflow-x-auto rounded-btn border border-border">
      <table className="w-full text-xs border-collapse">
        <thead>
          <tr className="bg-elevated/40 text-muted">
            <th className="px-3 py-2 text-left font-medium w-12">#</th>
            <th className="px-3 py-2 text-left font-medium">标的</th>
            <th className="px-3 py-2 text-left font-medium w-20">买点</th>
            <th className="px-3 py-2 text-right font-medium w-20">距今</th>
            <th className="px-3 py-2 text-right font-medium w-24">信号价</th>
            <th className="px-3 py-2 text-left font-medium w-16">趋势</th>
            <th className="px-3 py-2 text-right font-medium w-40">中枢 ZD ~ ZG</th>
            <th className="px-3 py-2 text-left font-medium">结构说明</th>
            <th className="px-3 py-2 w-8" />
          </tr>
        </thead>
        <tbody>
          {items.map((it, idx) => {
            const trend = TREND_META[it.trend] ?? { label: it.trend, cls: 'text-muted' }
            return (
              <tr
                key={`${it.symbol}-${it.kind}-${it.bars_since}`}
                onClick={() => onPreview(it)}
                className="border-t border-border/60 hover:bg-elevated/40 cursor-pointer transition-colors"
              >
                <td className="px-3 py-1.5 text-muted num tabular-nums">{idx + 1}</td>
                <td className="px-3 py-1.5">
                  <span className="font-mono text-secondary">{it.symbol}</span>
                  {it.name && <span className="ml-2 text-[11px] text-muted">{it.name}</span>}
                </td>
                <td className="px-3 py-1.5">
                  <span className={`inline-block px-1.5 py-px rounded text-[10px] font-semibold border ${KIND_CLS[it.kind] ?? KIND_FALLBACK_CLS}`}>
                    {it.label}
                  </span>
                </td>
                <td className="px-3 py-1.5 text-right num tabular-nums text-secondary">{it.bars_since} 根</td>
                <td className="px-3 py-1.5 text-right num tabular-nums text-foreground">{fmtPrice(it.price)}</td>
                <td className={`px-3 py-1.5 ${trend.cls}`}>{trend.label}</td>
                <td className="px-3 py-1.5 text-right num tabular-nums text-muted">
                  {it.center_zd != null && it.center_zg != null
                    ? `${it.center_zd.toFixed(2)} ~ ${it.center_zg.toFixed(2)}`
                    : '—'}
                </td>
                <td className="px-3 py-1.5 text-[11px] text-muted truncate max-w-[280px]" title={it.text}>
                  {it.text}
                </td>
                <td className="px-3 py-1.5 text-muted">
                  <ExternalLink className="h-3 w-3" />
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export function ChanScan() {
  const { market } = useMarket()
  const qc = useQueryClient()
  const [kinds, setKinds] = useState<string[]>(['3buy'])
  const [recentBars, setRecentBars] = useState(15)
  const [strict, setStrict] = useState(true)
  // null = 还没扫过；有值 = 用这份参数去扫（重复点同一组参数走 refetch）
  const [params, setParams] = useState<ScanParams | null>(null)
  const [preview, setPreview] = useState<{ symbol: string; name: string } | null>(null)

  const kindsKey = useMemo(() => [...kinds].sort().join(','), [kinds])

  const scan = useQuery({
    queryKey: QK.chanScan(kindsKey, recentBars, strict),
    queryFn: () => api.chanScan(kindsKey, LOOKBACK, recentBars, strict, LIMIT),
    enabled: !!params && kinds.length > 0,
    // 扫描结果按日不变（盘后数据），5 分钟内不重算
    staleTime: 5 * 60_000,
    retry: false,
  })

  const toggleKind = (key: string) => {
    setKinds(prev => (prev.includes(key) ? prev.filter(k => k !== key) : [...prev, key]))
  }

  const runScan = () => {
    if (kinds.length === 0) return
    const next: ScanParams = { kinds, recentBars, strict }
    const same = !!params
      && params.recentBars === next.recentBars
      && params.strict === next.strict
      && [...params.kinds].sort().join(',') === kindsKey
    setParams(next)
    // 同参数再点 = 显式刷新：先失效缓存再取
    if (same) {
      qc.invalidateQueries({ queryKey: QK.chanScan(kindsKey, recentBars, strict) })
    }
  }

  const data = scan.data
  const items = data?.items ?? []
  /** 各类型命中数（后端按 limit 截断，这里统计的是返回集） */
  const hitByKind = useMemo(() => {
    const acc: Record<string, number> = {}
    for (const it of items) acc[it.kind] = (acc[it.kind] ?? 0) + 1
    return acc
  }, [items])

  return (
    <div className="flex flex-col h-full">
      <PageHeader
        title="缠论买卖点扫描"
        subtitle={
          market === 'cn'
            ? '全市场日K · 笔/中枢/一二三买卖点'
            : '缠论扫描目前仅覆盖 A 股（日K enriched 源）'
        }
        right={
          <div className="flex items-center gap-2">
            {data && (
              <span className="text-[11px] text-muted num tabular-nums">
                扫描 {data.scanned.toLocaleString()} 只 · 命中 {data.hit_count} · 用时{' '}
                {(data.elapsed_ms / 1000).toFixed(1)}s
                {data.frame_ms != null && ` (取数 ${(data.frame_ms / 1000).toFixed(1)}s)`}
              </span>
            )}
            <button
              onClick={runScan}
              disabled={scan.isFetching || kinds.length === 0}
              className="inline-flex items-center gap-1.5 h-7 px-3 rounded-btn text-xs font-medium
                text-accent border border-accent/25 bg-accent/5 hover:bg-accent/15
                transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-default"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${scan.isFetching ? 'animate-spin' : ''}`} />
              {scan.isFetching ? '扫描中…' : params ? '重新扫描' : '开始扫描'}
            </button>
          </div>
        }
      />

      <div className="px-8 py-4 space-y-3 overflow-y-auto">
        {/* 参数区 */}
        <section className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-btn border border-border bg-surface/60 px-4 py-2.5">
          <div className="flex items-center gap-1.5">
            <span className="text-[11px] text-muted">买点</span>
            {BUY_KIND_OPTIONS.map(opt => {
              const on = kinds.includes(opt.key)
              return (
                <button
                  key={opt.key}
                  onClick={() => toggleKind(opt.key)}
                  className={`px-2 py-0.5 rounded text-[11px] font-medium border transition-colors cursor-pointer ${
                    on ? opt.cls : 'bg-elevated/60 text-muted border-border hover:text-secondary'
                  }`}
                >
                  {opt.label}
                </button>
              )
            })}
          </div>

          <div className="flex items-center gap-1.5">
            <span className="text-[11px] text-muted">卖点</span>
            {SELL_KIND_OPTIONS.map(opt => {
              const on = kinds.includes(opt.key)
              return (
                <button
                  key={opt.key}
                  onClick={() => toggleKind(opt.key)}
                  className={`px-2 py-0.5 rounded text-[11px] font-medium border transition-colors cursor-pointer ${
                    on ? opt.cls : 'bg-elevated/60 text-muted border-border hover:text-secondary'
                  }`}
                >
                  {opt.label}
                </button>
              )
            })}
            {kinds.length === 0 && <span className="text-[10px] text-warning/90">至少选一个</span>}
          </div>

          <div className="flex items-center gap-1.5">
            <span className="text-[11px] text-muted">新鲜度</span>
            <select
              value={recentBars}
              onChange={e => setRecentBars(Number(e.target.value))}
              className="h-6 rounded border border-border bg-base px-1.5 text-[11px] text-secondary outline-none"
            >
              {FRESHNESS_OPTIONS.map(v => (
                <option key={v} value={v}>距今 ≤ {v} 根</option>
              ))}
            </select>
          </div>

          <div className="flex items-center gap-1.5">
            <span className="text-[11px] text-muted">笔</span>
            <div className="flex rounded overflow-hidden border border-border">
              <button
                onClick={() => setStrict(true)}
                title="严格笔：顶底分型间至少间隔 4 根合并K线（最通行口径）"
                className={`px-2 py-0.5 text-[11px] transition-colors cursor-pointer ${
                  strict ? 'bg-accent/15 text-accent' : 'bg-elevated text-muted hover:text-secondary'
                }`}
              >
                严格
              </button>
              <button
                onClick={() => setStrict(false)}
                title="宽松笔（新笔）：间隔 3 根，笔更多、信号更密"
                className={`px-2 py-0.5 text-[11px] border-l border-border transition-colors cursor-pointer ${
                  !strict ? 'bg-accent/15 text-accent' : 'bg-elevated text-muted hover:text-secondary'
                }`}
              >
                宽松
              </button>
            </div>
          </div>

          <div className="flex items-center gap-1 text-[10.5px] text-muted">
            <Info className="h-3 w-3" />
            信号价 = 买卖点确认时的价格；中枢区间是该信号所属中枢的 ZD ~ ZG
          </div>
        </section>

        {/* 命中分布 */}
        {data && items.length > 0 && (
          <section className="flex items-center gap-3 text-[11px] text-muted">
            <span>命中分布</span>
            {KIND_OPTIONS.map(opt => {
              const n = hitByKind[opt.key] ?? 0
              if (n === 0) return null
              return (
                <span key={opt.key} className={`inline-flex items-center gap-1 rounded border px-1.5 py-px ${opt.cls}`}>
                  {opt.label} <b className="num">{n}</b>
                </span>
              )
            })}
            {data.hit_count > items.length && (
              <span className="text-warning/90">
                仅展示前 {items.length} / {data.hit_count} 只（按信号新鲜度排序）
              </span>
            )}
          </section>
        )}

        {/* 结果 */}
        {!params ? (
          <EmptyState
            icon={ScanSearch}
            title="选择买点类型后点击「开始扫描」"
            hint="冷启动需遍历全市场约 5500 只日K，约 20 秒；结果会被缓存，重复查询瞬时返回。"
          />
        ) : scan.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-muted py-10 justify-center">
            <RefreshCw className="h-4 w-4 animate-spin" />
            正在遍历全市场日K计算缠论结构…
          </div>
        ) : scan.isError ? (
          <div className="text-sm text-danger py-4">扫描失败：{(scan.error as Error)?.message ?? '未知错误'}</div>
        ) : items.length === 0 ? (
          <EmptyState
            icon={ScanSearch}
            title="无命中"
            hint={`当前条件下全市场没有距今 ${recentBars} 根内的${kinds.map(k => KIND_OPTIONS.find(o => o.key === k)?.label).join('/')}。可放宽新鲜度或改选其它买卖点类型。`}
          />
        ) : (
          <motion.div initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}>
            <ChanScanResultTable
              items={items}
              onPreview={it => setPreview({ symbol: it.symbol, name: it.name ?? '' })}
            />
          </motion.div>
        )}
      </div>

      <StockPreviewDialog
        symbol={preview?.symbol ?? null}
        name={preview?.name}
        onClose={() => setPreview(null)}
        chanOverlay
      />
    </div>
  )
}
