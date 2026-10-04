/**
 * 盘中脉搏（Pulse）。
 *
 * 面板原来只有「日K + 分钟K + 实时快照」这一层行情，缺的是**盘口背后那半边**——
 * 谁在买、买得多急、封得死不死、题材热不热。这一页把 eltdx（通达信 7709）的
 * 六组能力一次性补齐（对应后端 app/pulse/）：
 *
 *   M1 资金流     主力净额 / 超大·大·中·小单分档，全市场排行
 *   M2 集合竞价   09:15~09:25 匹配序列 + 开盘量额 + 强度评分
 *   M3 买卖力道   逐分钟主买/主卖 + 累计净力道
 *   M4 题材热度   题材强度榜（涨停数/最高连板/封单额/龙头）+ 个股所属题材
 *   M5 涨停梯队   连板高度 / 封单 / 开板状态
 *   M6 逐笔订单流 Delta·CumDelta + 足迹图（价格 x 时间的买卖量网格）
 *
 * 统一量纲：金额=元，比率=小数（0.0968 即 9.68%），成交量=手。
 *
 * ⚠️ M4/M5 上游要跑 137s / 113s，后端走 TTL 缓存：命中过期会**立刻返回旧值**并
 *    标 stale=true，后台刷新。所以这里看到 stale 提示不用等，刷新会自动发生。
 */
import { useMemo, useRef, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Activity, Loader2, RefreshCw, Flame, Layers, Gavel, Grid3x3, Waves, BarChart3,
} from 'lucide-react'
import {
  api,
  type PulseAuction,
  type PulseFootprint,
  type PulseMoneyflowRankRow,
  type PulseOrderflow,
} from '@/lib/api'   // PulseFootprint / PulseAuction / PulseLadder 见下方组件签名
import { QK } from '@/lib/queryKeys'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'

type TabKey = 'moneyflow' | 'auction' | 'strength' | 'topics' | 'ladder' | 'flow' | 'pricedist'

const TABS: { key: TabKey; label: string; icon: typeof Activity }[] = [
  { key: 'moneyflow', label: '资金流', icon: Waves },
  { key: 'auction', label: '集合竞价', icon: Gavel },
  { key: 'strength', label: '买卖力道', icon: Activity },
  { key: 'topics', label: '题材热度', icon: Flame },
  { key: 'ladder', label: '涨停梯队', icon: Layers },
  { key: 'flow', label: '逐笔/足迹', icon: Grid3x3 },
  { key: 'pricedist', label: '分价表', icon: BarChart3 },
]

/** 金额：元 -> 亿/万，保留符号。 */
function amountText(v: unknown, digits = 2): string {
  if (v == null || typeof v !== 'number' || Number.isNaN(v)) return '--'
  const abs = Math.abs(v)
  const sign = v < 0 ? '-' : ''
  if (abs >= 1e8) return `${sign}${(abs / 1e8).toFixed(digits)}亿`
  if (abs >= 1e4) return `${sign}${(abs / 1e4).toFixed(digits)}万`
  return `${sign}${abs.toFixed(0)}`
}

function pctText(v: unknown, digits = 2): string {
  if (v == null || typeof v !== 'number' || Number.isNaN(v)) return '--'
  return `${v >= 0 ? '+' : ''}${(v * 100).toFixed(digits)}%`
}

/** 涨红跌绿（A 股口径）。 */
function pctClass(v: unknown): string {
  if (v == null || typeof v !== 'number' || Number.isNaN(v) || v === 0) return 'text-secondary'
  return v > 0 ? 'text-bull' : 'text-bear'
}

function numText(v: unknown, digits = 2): string {
  if (v == null || typeof v !== 'number' || Number.isNaN(v)) return '--'
  return v.toFixed(digits)
}

export function Pulse() {
  const [tab, setTab] = useState<TabKey>('moneyflow')
  const [symbol, setSymbol] = useState('600519.SH')

  return (
    <div className="flex flex-col h-full">
      <PageHeader
        title="盘中脉搏"
        subtitle="资金流 / 竞价 / 力道 / 题材 / 涨停梯队 / 逐笔订单流 · 数据源 eltdx(通达信)"
      />

      <div className="flex items-center gap-2 px-5 py-3 border-b border-border flex-wrap">
        {TABS.map((t) => {
          const Icon = t.icon
          const active = tab === t.key
          return (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={`flex items-center gap-1.5 h-7 px-3 text-xs rounded-btn border cursor-pointer ${
                active
                  ? 'bg-accent text-white border-accent'
                  : 'border-border bg-surface text-secondary hover:text-foreground'
              }`}
            >
              <Icon className="h-3.5 w-3.5" />
              {t.label}
            </button>
          )
        })}

        {tab !== 'moneyflow' && tab !== 'topics' && tab !== 'ladder' && (
          <SymbolSearchInput value={symbol} onChange={setSymbol} />
        )}
      </div>

      <div className="flex-1 overflow-y-auto px-5 py-4">
        {tab === 'moneyflow' && <MoneyflowPanel />}
        {tab === 'auction' && <AuctionPanel symbol={symbol} />}
        {tab === 'strength' && <StrengthPanel symbol={symbol} />}
        {tab === 'topics' && <TopicsPanel symbol={symbol} />}
        {tab === 'ladder' && <LadderPanel />}
        {tab === 'flow' && <FlowPanel symbol={symbol} />}
        {tab === 'pricedist' && <PriceDistPanel symbol={symbol} />}
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ M1 资金流 */

function MoneyflowPanel() {
  const [days, setDays] = useState(1)
  const [scan, setScan] = useState(2000)
  const [asc, setAsc] = useState(false)

  const { data, isFetching, refetch } = useQuery({
    queryKey: [...QK.pulse, 'moneyflow-rank', days, scan, asc],
    queryFn: () => api.pulseMoneyflowRank({ limit: 100, days, scan, ascending: asc }),
  })

  const rows = data?.rows ?? []
  const maxAbs = useMemo(
    () => Math.max(1, ...rows.map((r) => Math.abs(r.main_net ?? 0))),
    [rows],
  )

  return (
    <section className="space-y-3">
      <div className="flex items-center gap-3 flex-wrap text-[11px] text-muted">
        <label className="flex items-center gap-1.5">
          区间(交易日)
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="h-7 px-2 text-xs rounded-btn border border-border bg-surface text-foreground"
          >
            {[1, 3, 5, 10].map((d) => <option key={d} value={d}>{d} 日</option>)}
          </select>
        </label>
        <label className="flex items-center gap-1.5">
          扫描
          <select
            value={scan}
            onChange={(e) => setScan(Number(e.target.value))}
            className="h-7 px-2 text-xs rounded-btn border border-border bg-surface text-foreground"
          >
            {[500, 2000, 5000, 8000].map((n) => <option key={n} value={n}>{n} 只</option>)}
          </select>
        </label>
        <button
          onClick={() => setAsc(!asc)}
          className="h-7 px-3 text-xs rounded-btn border border-border bg-surface hover:text-foreground cursor-pointer"
        >
          {asc ? '净流出榜' : '净流入榜'}
        </button>
        <button
          onClick={() => void refetch()}
          className="flex items-center gap-1 h-7 px-3 text-xs rounded-btn border border-border bg-surface hover:text-foreground cursor-pointer"
        >
          {isFetching ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
          刷新
        </button>
        <span>已扫描 {data?.scanned ?? 0} 只</span>
      </div>

      {rows.length === 0 && !isFetching ? (
        <EmptyState
          icon={Waves}
          title="暂无资金流数据"
          hint="资金流来自通达信 money_flow 接口。非交易时段上游可能为空，换个交易日或先在设置页确认 eltdx 已连通。"
        />
      ) : (
        <div className="rounded-card border border-border overflow-hidden">
          <table className="w-full text-xs border-collapse">
            <thead className="bg-surface-2">
              <tr className="text-muted">
                <th className="px-3 py-2 text-left font-medium">代码</th>
                <th className="px-3 py-2 text-left font-medium">名称</th>
                <th className="px-3 py-2 text-right font-medium">主力净额</th>
                <th className="px-3 py-2 text-left font-medium">强度</th>
                <th className="px-3 py-2 text-right font-medium">占成交额</th>
                <th className="px-3 py-2 text-right font-medium">超大单</th>
                <th className="px-3 py-2 text-right font-medium">大单</th>
                <th className="px-3 py-2 text-right font-medium">中单</th>
                <th className="px-3 py-2 text-right font-medium">小单</th>
                <th className="px-3 py-2 text-right font-medium">成交额</th>
                {days > 1 && <th className="px-3 py-2 text-right font-medium">区间累计</th>}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <MoneyflowRow key={r.symbol} r={r} maxAbs={maxAbs} days={days} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

function MoneyflowRow({ r, maxAbs, days }: { r: PulseMoneyflowRankRow; maxAbs: number; days: number }) {
  const net = r.main_net ?? 0
  const w = Math.min(100, (Math.abs(net) / maxAbs) * 100)
  return (
    <tr className="border-t border-border hover:bg-surface-2">
      <td className="px-3 py-1.5 font-medium">
        <Link
          to={`/stock/${encodeURIComponent(r.symbol)}`}
          className="text-accent hover:underline"
          title={`查看 ${r.symbol} K线`}
        >
          {r.symbol}
        </Link>
      </td>
      <td className="px-3 py-1.5 text-muted">{r.name ?? '--'}</td>
      <td className={`px-3 py-1.5 text-right num tabular-nums ${pctClass(net)}`}>{amountText(net)}</td>
      <td className="px-3 py-1.5 w-[120px]">
        <div className="h-1.5 bg-surface-2 rounded-full overflow-hidden">
          <div
            className={`h-full ${net >= 0 ? 'bg-bull' : 'bg-bear'}`}
            style={{ width: `${w}%`, marginLeft: net >= 0 ? 0 : `${100 - w}%` }}
          />
        </div>
      </td>
      <td className={`px-3 py-1.5 text-right num tabular-nums ${pctClass(r.main_ratio)}`}>{pctText(r.main_ratio)}</td>
      <td className={`px-3 py-1.5 text-right num tabular-nums ${pctClass(r.super_large_net)}`}>{amountText(r.super_large_net)}</td>
      <td className={`px-3 py-1.5 text-right num tabular-nums ${pctClass(r.large_net)}`}>{amountText(r.large_net)}</td>
      <td className={`px-3 py-1.5 text-right num tabular-nums ${pctClass(r.medium_net)}`}>{amountText(r.medium_net)}</td>
      <td className={`px-3 py-1.5 text-right num tabular-nums ${pctClass(r.small_net)}`}>{amountText(r.small_net)}</td>
      <td className="px-3 py-1.5 text-right num tabular-nums text-muted">{amountText(r.total_amount)}</td>
      {days > 1 && (
        <td className={`px-3 py-1.5 text-right num tabular-nums ${pctClass(r.sum_main_net)}`}>
          {amountText(r.sum_main_net)}
          <span className="ml-1 text-[10px] text-muted">{r.inflow_days}/{r.days}日流入</span>
        </td>
      )}
    </tr>
  )
}

/* ------------------------------------------------------------------ M2 集合竞价 */

function AuctionPanel({ symbol }: { symbol: string }) {
  const [view, setView] = useState<'single' | 'scan'>('single')

  return (
    <section className="space-y-3">
      <div className="flex items-center gap-2">
        {([['single', '单只详情'], ['scan', '全市场扫描']] as const).map(([k, label]) => (
          <button
            key={k}
            onClick={() => setView(k)}
            className={`h-7 px-3 text-xs rounded-btn border cursor-pointer ${
              view === k
                ? 'bg-accent text-white border-accent'
                : 'border-border bg-surface text-secondary hover:text-foreground'
            }`}
          >
            {label}
          </button>
        ))}
        {view === 'scan' && (
          <span className="text-[11px] text-muted">
            上游竞价序列只保留最近一个交易日；非交易日看到的是上一交易日数据
          </span>
        )}
      </div>
      {view === 'single' ? <AuctionSingle symbol={symbol} /> : <AuctionScan />}
    </section>
  )
}

function AuctionSingle({ symbol }: { symbol: string }) {
  const { data, isFetching } = useQuery({
    queryKey: [...QK.pulse, 'auction', symbol],
    queryFn: () => api.pulseAuction(symbol),
    enabled: !!symbol,
  })
  return (
    <SingleBlock loading={isFetching} empty={!data || data.points.length === 0} hint="非竞价时段（09:15~09:25 之外）上游不返回序列，个股题材与资金流不受影响。">
      {data && data.points.length > 0 && (
        <div className="space-y-4">
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
            <Metric label="开盘价" value={numText(data.open_price)} cls={pctClass(data.open_change_pct)} />
            <Metric label="开盘涨幅" value={pctText(data.open_change_pct)} cls={pctClass(data.open_change_pct)} />
            <Metric label="竞价量" value={`${numText(data.open_volume, 0)} 手`} />
            <Metric label="竞价额" value={amountText(data.open_amount)} />
            <Metric label="强度评分" value={data.score ? `${data.score.total}` : '--'} />
          </div>

          {data.score && (
            <div>
              <div className="text-xs font-medium mb-1.5">评分分项</div>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[11px]">
                <PartCell name="加速(后段/前段)" v={data.score.accel} full={data.score.parts.accel} max={30} />
                <PartCell name="不撤单" v={data.score.cancel_rate == null ? null : 1 - data.score.cancel_rate} full={data.score.parts.cancel} max={25} />
                <PartCell name="价格稳定" v={data.score.stability} full={data.score.parts.stability} max={20} />
                <PartCell name="量能(bp)" v={data.score.open_turnover_bp} full={data.score.parts.volume} max={25} />
              </div>
            </div>
          )}

          <div>
            <div className="text-xs font-medium mb-1.5">
              匹配序列
              <span className="ml-2 text-[11px] font-normal text-muted">
                蓝=累计匹配量(手) · 灰=未匹配量(手) · 撤单率只统计 09:20 之前
              </span>
            </div>
            <AuctionChart data={data} />
          </div>
        </div>
      )}
    </SingleBlock>
  )
}

/** 全市场竞价扫描: 竞价强度榜 / 低开走强榜。 */
function AuctionScan() {
  const [scan, setScan] = useState(500)
  const [mode, setMode] = useState<'score' | 'repair'>('score')
  const [asc, setAsc] = useState(false)

  const { data, isFetching, refetch } = useQuery({
    queryKey: [...QK.pulse, 'auction-scan', scan, mode, asc],
    queryFn: () => api.pulseAuctionScan({ scan, mode, limit: 100, ascending: asc }),
    staleTime: 60_000,
  })

  const rows = data?.rows ?? []

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3 flex-wrap text-[11px] text-muted">
        <label className="flex items-center gap-1.5">
          扫描
          <select
            value={scan}
            onChange={(e) => setScan(Number(e.target.value))}
            className="h-7 px-2 text-xs rounded-btn border border-border bg-surface text-foreground"
          >
            {[500, 2000, 5000].map((n) => <option key={n} value={n}>{n} 只</option>)}
          </select>
        </label>
        <button
          onClick={() => setMode(mode === 'score' ? 'repair' : 'score')}
          className="h-7 px-3 text-xs rounded-btn border border-border bg-surface hover:text-foreground cursor-pointer"
        >
          {mode === 'score' ? '竞价强度榜' : '低开走强榜'}
        </button>
        <button
          onClick={() => setAsc(!asc)}
          className="h-7 px-3 text-xs rounded-btn border border-border bg-surface hover:text-foreground cursor-pointer"
        >
          {asc ? '升序' : '降序'}
        </button>
        <button
          onClick={() => void refetch()}
          className="flex items-center gap-1 h-7 px-3 text-xs rounded-btn border border-border bg-surface hover:text-foreground cursor-pointer"
        >
          {isFetching ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
          刷新
        </button>
        <span>交易日 {data?.date ?? '--'} · 已扫描 {data?.scanned ?? 0} 只 / 可评分 {data?.scored ?? 0} 只</span>
        {mode === 'repair' && (
          <span className="text-warning">
            · 只保留竞价低开的票，按「当前涨幅 − 开盘涨幅」排序
          </span>
        )}
      </div>

      {rows.length === 0 && !isFetching ? (
        <EmptyState
          icon={Gavel}
          title="暂无竞价数据"
          hint="竞价序列只保留最近一个交易日。若当前非交易日，上游返回的是上一交易日数据；换个扫描量或稍后再试。"
        />
      ) : (
        <div className="rounded-card border border-border overflow-hidden">
          <table className="w-full text-xs border-collapse">
            <thead className="bg-surface-2">
              <tr className="text-muted">
                <th className="px-3 py-2 text-left font-medium">代码</th>
                <th className="px-3 py-2 text-left font-medium">名称</th>
                <th className="px-3 py-2 text-right font-medium">开盘涨幅</th>
                <th className="px-3 py-2 text-right font-medium">当前涨幅</th>
                <th className="px-3 py-2 text-right font-medium">日内修复</th>
                <th className="px-3 py-2 text-right font-medium">竞价评分</th>
                <th className="px-3 py-2 text-right font-medium">加速</th>
                <th className="px-3 py-2 text-right font-medium">撤单率</th>
                <th className="px-3 py-2 text-right font-medium">稳定</th>
                <th className="px-3 py-2 text-right font-medium">量能bp</th>
                <th className="px-3 py-2 text-right font-medium">竞价额</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.symbol} className="border-t border-border hover:bg-surface-2">
                  <td className="px-3 py-1.5 font-medium">
                    <Link to={`/stock/${encodeURIComponent(r.symbol)}`} className="text-accent hover:underline">
                      {r.symbol}
                    </Link>
                  </td>
                  <td className="px-3 py-1.5 text-muted">
                    {r.name ?? '--'}
                    {r.is_limit_up && <span className="ml-1 text-bull font-medium">涨停</span>}
                  </td>
                  <td className={`px-3 py-1.5 text-right num tabular-nums ${pctClass(r.open_change_pct)}`}>
                    {pctText(r.open_change_pct)}
                  </td>
                  <td className={`px-3 py-1.5 text-right num tabular-nums ${pctClass(r.change_pct)}`}>
                    {r.change_pct == null ? '--' : pctText(r.change_pct)}
                  </td>
                  <td className={`px-3 py-1.5 text-right num tabular-nums ${pctClass(r.repair_pct)}`}>
                    {r.repair_pct == null ? '--' : pctText(r.repair_pct)}
                  </td>
                  <td className="px-3 py-1.5 text-right num tabular-nums">
                    {r.score == null ? <span className="text-muted">--</span> : r.score.toFixed(1)}
                  </td>
                  <td className="px-3 py-1.5 text-right num tabular-nums text-muted">{numText(r.accel)}</td>
                  <td className="px-3 py-1.5 text-right num tabular-nums text-muted">
                    {r.cancel_rate == null ? '--' : `${(r.cancel_rate * 100).toFixed(1)}%`}
                  </td>
                  <td className="px-3 py-1.5 text-right num tabular-nums text-muted">{numText(r.stability)}</td>
                  <td className="px-3 py-1.5 text-right num tabular-nums text-muted">{numText(r.open_turnover_bp)}</td>
                  <td className="px-3 py-1.5 text-right num tabular-nums text-muted">{amountText(r.open_amount)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="text-[11px] text-muted">
        量纲：涨幅/撤单率为小数展示（+1.20% 即 0.012）。竞价评分 = 加速 30 + (1−撤单) 25 + 稳定 20 + 量能 25；
        「日内修复」= 当前涨幅 − 开盘涨幅，开盘涨幅取竞价（权威）、当前涨幅取实时快照，衡量低开后的回补力度。
      </div>
    </div>
  )
}

function AuctionChart({ data }: { data: PulseAuction }) {
  const W = 900, H = 220, PL = 44, PR = 44, PT = 12, PB = 22
  const pts = data.points
  const maxM = Math.max(1, ...pts.map((p) => p.matched ?? 0))
  const maxU = Math.max(1, ...pts.map((p) => p.unmatched ?? 0))
  const x = (i: number) => PL + (i / Math.max(1, pts.length - 1)) * (W - PL - PR)
  const ym = (v: number) => PT + (1 - v / maxM) * (H - PT - PB)
  const yu = (v: number) => PT + (1 - v / maxU) * (H - PT - PB)
  const line = (fn: (p: (typeof pts)[number]) => number, map: (v: number) => number) =>
    pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${map(fn(p)).toFixed(1)}`).join(' ')
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-[220px]">
      <line x1={PL} y1={PT} x2={PL} y2={H - PB} stroke="currentColor" className="text-border" />
      <line x1={PL} y1={H - PB} x2={W - PR} y2={H - PB} stroke="currentColor" className="text-border" />
      <path d={line((p) => p.unmatched ?? 0, yu)} fill="none" stroke="currentColor" className="text-muted" strokeWidth={1} />
      <path d={line((p) => p.matched ?? 0, ym)} fill="none" className="text-accent" stroke="currentColor" strokeWidth={1.8} />
      <text x={2} y={PT + 10} className="fill-current text-muted" fontSize={10}>{Math.round(maxM)}</text>
      <text x={2} y={H - PB} className="fill-current text-muted" fontSize={10}>0</text>
      {pts.length > 1 && (
        <>
          <text x={PL} y={H - 6} className="fill-current text-muted" fontSize={10}>{pts[0].time}</text>
          <text x={W - PR - 30} y={H - 6} className="fill-current text-muted" fontSize={10}>{pts[pts.length - 1].time}</text>
        </>
      )}
    </svg>
  )
}

/* ------------------------------------------------------------------ M3 买卖力道 */

function StrengthPanel({ symbol }: { symbol: string }) {
  const { data, isFetching } = useQuery({
    queryKey: [...QK.pulse, 'strength', symbol],
    queryFn: () => api.pulseStrength(symbol),
    enabled: !!symbol,
  })
  const pts = data?.points ?? []
  const maxV = Math.max(1, ...pts.map((p) => Math.max(p.buy, p.sell)))
  return (
    <SingleBlock loading={isFetching} empty={pts.length === 0} hint="买卖力道来自通达信 buy_sell_strength，只覆盖当日 09:31~15:00。">
      {data?.summary && (
        <div className="space-y-4">
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
            <Metric label="主买合计" value={`${numText(data.summary.buy_total, 0)} 手`} cls="text-bull" />
            <Metric label="主卖合计" value={`${numText(data.summary.sell_total, 0)} 手`} cls="text-bear" />
            <Metric label="净力道" value={`${numText(data.summary.net, 0)} 手`} cls={pctClass(data.summary.net)} />
            <Metric label="强弱比" value={numText(data.summary.strength_ratio * 100, 1)} cls={pctClass(data.summary.strength_ratio)} />
            <Metric label="尾盘60分净额" value={`${numText(data.summary.tail_net, 0)} 手`} cls={pctClass(data.summary.tail_net)} />
          </div>
          <div>
            <div className="text-xs font-medium mb-1.5">
              逐分钟主买/主卖
              <span className="ml-2 text-[11px] font-normal text-muted">
                红=主买 · 绿=主卖 · 黄线=累计净力道（右轴）
              </span>
            </div>
            <StrengthChart pts={pts} maxV={maxV} />
          </div>
        </div>
      )}
    </SingleBlock>
  )
}

function StrengthChart({ pts, maxV }: { pts: { minute: string; buy: number; sell: number; cum_delta: number }[]; maxV: number }) {
  const W = 900, H = 240, PL = 44, PR = 48, PT = 12, PB = 22
  const bw = (W - PL - PR) / Math.max(1, pts.length)
  const cumAbs = Math.max(1, ...pts.map((p) => Math.abs(p.cum_delta)))
  const y = (v: number) => PT + (1 - v / maxV) * (H - PT - PB)
  const yc = (v: number) => PT + (1 - (v / cumAbs + 1) / 2) * (H - PT - PB)
  const cumPath = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${(PL + i * bw + bw / 2).toFixed(1)},${yc(p.cum_delta).toFixed(1)}`).join(' ')
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-[240px]">
      <line x1={PL} y1={PT} x2={PL} y2={H - PB} stroke="currentColor" className="text-border" />
      <line x1={PL} y1={H - PB} x2={W - PR} y2={H - PB} stroke="currentColor" className="text-border" />
      {pts.map((p, i) => {
        const cx = PL + i * bw + bw / 2
        return (
          <g key={p.minute}>
            <rect x={cx - bw * 0.42} y={y(p.buy)} width={Math.max(1, bw * 0.4)} height={Math.max(0, H - PB - y(p.buy))} className="fill-current text-bull" opacity={0.85} />
            <rect x={cx + bw * 0.02} y={y(p.sell)} width={Math.max(1, bw * 0.4)} height={Math.max(0, H - PB - y(p.sell))} className="fill-current text-bear" opacity={0.85} />
          </g>
        )
      })}
      <path d={cumPath} fill="none" className="text-accent" stroke="currentColor" strokeWidth={1.6} />
      <text x={2} y={PT + 10} className="fill-current text-muted" fontSize={10}>{Math.round(maxV)}</text>
      {pts.length > 0 && (
        <>
          <text x={PL} y={H - 6} className="fill-current text-muted" fontSize={10}>{pts[0].minute}</text>
          <text x={W - PR - 26} y={H - 6} className="fill-current text-muted" fontSize={10}>{pts[pts.length - 1].minute}</text>
        </>
      )}
    </svg>
  )
}

/* ------------------------------------------------------------------ M4 题材 */

function TopicsPanel({ symbol }: { symbol: string }) {
  const [includePseudo, setIncludePseudo] = useState(false)
  const { data, isFetching, refetch } = useQuery({
    queryKey: [...QK.pulse, 'topic-rank', includePseudo],
    queryFn: () => api.pulseTopicRank({ include_pseudo: includePseudo }),
    staleTime: 5 * 60_000,
  })
  const { data: mine } = useQuery({
    queryKey: [...QK.pulse, 'topics', symbol],
    queryFn: () => api.pulseTopics(symbol),
    enabled: !!symbol,
  })

  return (
    <section className="space-y-4">
      <div className="flex items-center gap-3 flex-wrap text-[11px] text-muted">
        <span>交易日 {data?.trade_date ?? '--'}</span>
        {data?.stale && <span className="text-warning">· 后台刷新中（当前为上一次结果）</span>}
        <label className="flex items-center gap-1.5">
          <input type="checkbox" checked={includePseudo} onChange={(e) => setIncludePseudo(e.target.checked)} />
          含统计标签（昨日涨停等）
        </label>
        <button
          onClick={() => void refetch()}
          className="flex items-center gap-1 h-7 px-3 text-xs rounded-btn border border-border bg-surface hover:text-foreground cursor-pointer"
        >
          {isFetching ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
          刷新
        </button>
        <span>首次拉取约需 2 分钟，之后走缓存</span>
      </div>

      {mine && mine.topics.length > 0 && (
        <div>
          <div className="text-xs font-medium mb-1.5">{symbol} 所属题材</div>
          <div className="flex flex-wrap gap-1.5">
            {mine.topics.map((t) => (
              <span key={t.topic_id} className="px-2 py-1 text-[11px] rounded-btn border border-border bg-surface">
                {t.topic_name}
                <span className="ml-1 text-muted">L{t.relation_level ?? '-'}</span>
              </span>
            ))}
          </div>
        </div>
      )}

      <div className="rounded-card border border-border overflow-hidden">
        <table className="w-full text-xs border-collapse">
          <thead className="bg-surface-2">
            <tr className="text-muted">
              <th className="px-3 py-2 text-left font-medium">#</th>
              <th className="px-3 py-2 text-left font-medium">题材</th>
              <th className="px-3 py-2 text-right font-medium">涨停数</th>
              <th className="px-3 py-2 text-right font-medium">最高连板</th>
              <th className="px-3 py-2 text-right font-medium">连板数</th>
              <th className="px-3 py-2 text-right font-medium">封单额</th>
              <th className="px-3 py-2 text-left font-medium">龙头</th>
            </tr>
          </thead>
          <tbody>
            {(data?.rows ?? []).map((r) => (
              <tr key={r.topic_id} className="border-t border-border hover:bg-surface-2">
                <td className="px-3 py-1.5 text-muted">{r.rank ?? '-'}</td>
                <td className="px-3 py-1.5 font-medium">{r.topic_name}</td>
                <td className="px-3 py-1.5 text-right num tabular-nums">{r.limit_up_count ?? '--'}</td>
                <td className="px-3 py-1.5 text-right num tabular-nums">{r.highest_ladder_level ?? '--'}</td>
                <td className="px-3 py-1.5 text-right num tabular-nums">{r.lianban_count ?? '--'}</td>
                <td className="px-3 py-1.5 text-right num tabular-nums">{amountText(r.total_seal_amount)}</td>
                <td className="px-3 py-1.5">
                  {r.leader_name ? `${r.leader_name} ${r.leader_symbol}` : (r.leader_symbol ?? '--')}
                  {r.leader_ladder_level ? <span className="ml-1 text-muted">{r.leader_ladder_level}板</span> : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

/* ------------------------------------------------------------------ M5 涨停梯队 */

function LadderPanel() {
  const [minLevel, setMinLevel] = useState(1)
  const [onlySealed, setOnlySealed] = useState(false)
  const { data, isFetching, refetch } = useQuery({
    queryKey: [...QK.pulse, 'ladder', minLevel, onlySealed],
    queryFn: () => api.pulseLadder({ min_level: minLevel, only_sealed: onlySealed, limit: 300 }),
    staleTime: 5 * 60_000,
  })

  return (
    <section className="space-y-3">
      <div className="flex items-center gap-3 flex-wrap text-[11px] text-muted">
        <span>交易日 {data?.trade_date ?? '--'}</span>
        {data?.stale && <span className="text-warning">· 后台刷新中</span>}
        <label className="flex items-center gap-1.5">
          最低连板
          <select
            value={minLevel}
            onChange={(e) => setMinLevel(Number(e.target.value))}
            className="h-7 px-2 text-xs rounded-btn border border-border bg-surface text-foreground"
          >
            {[1, 2, 3, 4, 5].map((n) => <option key={n} value={n}>{n} 板及以上</option>)}
          </select>
        </label>
        <label className="flex items-center gap-1.5">
          <input type="checkbox" checked={onlySealed} onChange={(e) => setOnlySealed(e.target.checked)} />
          只看封住
        </label>
        <button
          onClick={() => void refetch()}
          className="flex items-center gap-1 h-7 px-3 text-xs rounded-btn border border-border bg-surface hover:text-foreground cursor-pointer"
        >
          {isFetching ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
          刷新
        </button>
        <span>首次拉取约需 2 分钟</span>
      </div>

      {data?.stats && (
        <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
          <Metric label="涨停家数" value={`${data.stats.count}`} />
          <Metric label="封住" value={`${data.stats.sealed}`} cls="text-bull" />
          <Metric label="最高板" value={`${data.stats.max_level}`} />
          <Metric label="封单总额" value={amountText(data.stats.seal_total_amount)} />
          <Metric label="总样本" value={`${data.total}`} />
        </div>
      )}

      <div className="rounded-card border border-border overflow-hidden">
        <table className="w-full text-xs border-collapse">
          <thead className="bg-surface-2">
            <tr className="text-muted">
              <th className="px-3 py-2 text-left font-medium">代码</th>
              <th className="px-3 py-2 text-left font-medium">名称</th>
              <th className="px-3 py-2 text-left font-medium">梯队</th>
              <th className="px-3 py-2 text-left font-medium">状态</th>
              <th className="px-3 py-2 text-right font-medium">开盘涨幅</th>
              <th className="px-3 py-2 text-right font-medium">封单额</th>
              <th className="px-3 py-2 text-right font-medium">封单/成交</th>
              <th className="px-3 py-2 text-right font-medium">封单/流通</th>
              <th className="px-3 py-2 text-right font-medium">今年涨停</th>
              <th className="px-3 py-2 text-right font-medium">PE(TTM)</th>
            </tr>
          </thead>
          <tbody>
            {(data?.rows ?? []).map((r) => (
              <tr key={r.symbol} className="border-t border-border hover:bg-surface-2">
                <td className="px-3 py-1.5">{r.symbol}</td>
                <td className="px-3 py-1.5 font-medium">{r.name ?? '--'}</td>
                <td className="px-3 py-1.5">
                  <span className="text-bull font-medium">{r.limit_board_text || `${r.ladder_level ?? '-'}板`}</span>
                </td>
                <td className="px-3 py-1.5">
                  <span className={r.limit_status === 'sealed' ? 'text-bull' : 'text-warning'}>
                    {r.limit_status === 'sealed' ? '封住' : r.limit_status || '--'}
                  </span>
                </td>
                <td className={`px-3 py-1.5 text-right num tabular-nums ${pctClass(r.open_change_pct)}`}>{pctText(r.open_change_pct)}</td>
                <td className="px-3 py-1.5 text-right num tabular-nums">{amountText(r.seal_amount)}</td>
                <td className="px-3 py-1.5 text-right num tabular-nums text-muted">{numText(r.seal_to_amount_ratio)}</td>
                <td className="px-3 py-1.5 text-right num tabular-nums text-muted">{numText(r.seal_to_float_ratio)}</td>
                <td className="px-3 py-1.5 text-right num tabular-nums text-muted">{r.year_limit_up_days ?? '--'}</td>
                <td className="px-3 py-1.5 text-right num tabular-nums text-muted">{numText(r.pe_ttm, 1)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

/* ------------------------------------------------------------------ M6 逐笔 / 足迹图 */

function FlowPanel({ symbol }: { symbol: string }) {
  const [rowsN, setRowsN] = useState(30)
  const [bucket, setBucket] = useState(15)

  const { data: of, isFetching: ofLoading } = useQuery({
    queryKey: [...QK.pulse, 'orderflow', symbol],
    queryFn: () => api.pulseOrderflow(symbol),
    enabled: !!symbol,
  })
  const { data: fp, isFetching: fpLoading } = useQuery({
    queryKey: [...QK.pulse, 'footprint', symbol, rowsN, bucket],
    queryFn: () => api.pulseFootprint(symbol, { rows: rowsN, bucket }),
    enabled: !!symbol,
  })

  return (
    <section className="space-y-4">
      <div className="flex items-center gap-3 flex-wrap text-[11px] text-muted">
        <span>交易日 {fp?.day ?? of?.day ?? '--'}</span>
        <label className="flex items-center gap-1.5">
          价格档
          <select
            value={rowsN}
            onChange={(e) => setRowsN(Number(e.target.value))}
            className="h-7 px-2 text-xs rounded-btn border border-border bg-surface text-foreground"
          >
            {[20, 30, 40, 60].map((n) => <option key={n} value={n}>{n} 档</option>)}
          </select>
        </label>
        <label className="flex items-center gap-1.5">
          时间桶
          <select
            value={bucket}
            onChange={(e) => setBucket(Number(e.target.value))}
            className="h-7 px-2 text-xs rounded-btn border border-border bg-surface text-foreground"
          >
            {[5, 15, 30].map((n) => <option key={n} value={n}>{n} 分钟</option>)}
          </select>
        </label>
        {of?.summary && (
          <>
            <span>成交 {of.summary.trades} 笔</span>
            <span className={pctClass(of.summary.delta)}>
              · Delta {numText(of.summary.delta, 0)} 手（{pctText(of.summary.delta_ratio)}）
            </span>
            <span>· VWAP {fp?.vwap ? numText(fp.vwap) : '--'}</span>
            <span>· POC {fp?.poc ? numText(fp.poc) : '--'}</span>
          </>
        )}
      </div>

      {ofLoading || fpLoading ? (
        <div className="flex items-center gap-2 text-xs text-muted py-6">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> 抓取逐笔中（首抓约 1~3 秒，之后走本地缓存）
        </div>
      ) : null}

      {fp && fp.cells.length > 0 ? (
        <div>
          <div className="text-xs font-medium mb-1.5">
            足迹图
            <span className="ml-2 text-[11px] font-normal text-muted">
              每格 = 该时间桶 × 该价格档的主动买卖量（红=主买 / 绿=主卖，亮度=量）
            </span>
          </div>
          <FootprintCanvas data={fp} />
        </div>
      ) : !fpLoading ? (
        <EmptyState
          icon={Grid3x3}
          title="暂无逐笔数据"
          hint="逐笔只保留最近一个交易日，且盘中才有累积。确认代码正确、或等到交易时段再看。"
        />
      ) : null}

      {of && of.points.length > 0 && (
        <div>
          <div className="text-xs font-medium mb-1.5">
            订单流 Delta / CumDelta
            <span className="ml-2 text-[11px] font-normal text-muted">黄线=累计 Delta（右轴）</span>
          </div>
          <OrderflowChart pts={of.points} />
        </div>
      )}
    </section>
  )
}

/* ---------------------------------------------------------------- S4 分价表 */

function PriceDistPanel({ symbol }: { symbol: string }) {
  const [rowsN, setRowsN] = useState(60)
  const { data, isFetching } = useQuery({
    queryKey: [...QK.pulse, 'pricedist', symbol, rowsN],
    queryFn: () => api.pulsePriceDist(symbol, { rows: rowsN }),
    enabled: !!symbol,
  })

  const maxVol = useMemo(
    () => Math.max(1, ...(data?.rows ?? []).map((r) => r.volume)),
    [data?.rows],
  )
  // 当前价落在哪一档: 用档区间判断(当前价未必正好等于档中值)
  const currentLevel = useMemo(() => {
    const cur = data?.current
    if (!data || cur == null) return null
    return data.rows.findIndex((r) => cur >= r.low && cur < r.high)
  }, [data])

  return (
    <section className="space-y-3">
      <div className="flex items-center gap-3 flex-wrap text-[11px] text-muted">
        <span>交易日 {data?.day ?? '--'}</span>
        {data?.updated && <span>· 快照 {data.updated.slice(11, 19)}</span>}
        <label className="flex items-center gap-1.5">
          价格档
          <select
            value={rowsN}
            onChange={(e) => setRowsN(Number(e.target.value))}
            className="h-7 px-2 text-xs rounded-btn border border-border bg-surface text-foreground"
          >
            {[20, 40, 60, 100].map((n) => <option key={n} value={n}>{n} 档</option>)}
          </select>
        </label>
        {data && data.rows.length > 0 && (
          <span>· 档宽 {numText(data.step)} 元（步长取 tick 整数倍，低价股自动退化成逐价位）</span>
        )}
      </div>

      {isFetching && !data ? (
        <div className="flex items-center gap-2 text-xs text-muted py-6">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> 抓取逐笔中（首抓约 1~3 秒，之后走本地缓存）
        </div>
      ) : null}

      {data && data.rows.length > 0 ? (
        <>
          <div className="grid grid-cols-2 sm:grid-cols-6 gap-3">
            <Metric label="成交量" value={`${numText(data.total_volume, 0)} 手`} />
            <Metric label="成交额" value={amountText(data.total_amount)} />
            <Metric label="VWAP" value={numText(data.vwap)} />
            <Metric label="POC（最密集价）" value={numText(data.poc)} cls="text-accent" />
            <Metric label="最新价" value={numText(data.current)} />
            <Metric
              label="主买/主卖"
              value={`${numText(data.buy_volume, 0)} / ${numText(data.sell_volume, 0)}`}
              cls={data.buy_volume >= data.sell_volume ? 'text-bull' : 'text-bear'}
            />
          </div>

          <div className="rounded-card border border-border overflow-hidden">
            <table className="w-full text-xs border-collapse">
              <thead className="bg-surface-2">
                <tr className="text-muted">
                  <th className="px-3 py-2 text-right font-medium">价格</th>
                  <th className="px-3 py-2 text-left font-medium">成交量分布（红=主买 / 绿=主卖）</th>
                  <th className="px-3 py-2 text-right font-medium">成交量(手)</th>
                  <th className="px-3 py-2 text-right font-medium">占比</th>
                  <th className="px-3 py-2 text-right font-medium">笔数</th>
                  <th className="px-3 py-2 text-right font-medium">主买</th>
                  <th className="px-3 py-2 text-right font-medium">主卖</th>
                  <th className="px-3 py-2 text-right font-medium">累计</th>
                </tr>
              </thead>
              <tbody>
                {/* 价格从高到低：与盘口习惯一致，当前价上方=套牢盘 */}
                {[...data.rows].map((r, i) => ({ r, i })).reverse().map(({ r, i: idx }) => {
                  const isCur = currentLevel === idx
                  const isPoc = data.poc != null && Math.abs(r.price - data.poc) < 1e-9
                  return (
                    <tr
                      key={r.price}
                      className={`border-t border-border ${isCur ? 'bg-accent/10' : ''}`}
                    >
                      <td className={`px-3 py-1 text-right font-mono ${isPoc ? 'text-accent font-medium' : ''}`}>
                        {numText(r.price)}
                        {isPoc && <span className="ml-1 text-[10px]">POC</span>}
                        {isCur && <span className="ml-1 text-[10px] text-muted">现价</span>}
                      </td>
                      <td className="px-3 py-1">
                        <div className="flex h-3 w-full overflow-hidden rounded-sm bg-surface-2">
                          <div
                            className="bg-bull"
                            style={{ width: `${(r.buy / maxVol) * 100}%` }}
                            title={`主买 ${numText(r.buy, 0)} 手`}
                          />
                          <div
                            className="bg-bear"
                            style={{ width: `${(r.sell / maxVol) * 100}%` }}
                            title={`主卖 ${numText(r.sell, 0)} 手`}
                          />
                        </div>
                      </td>
                      <td className="px-3 py-1 text-right font-mono">{numText(r.volume, 0)}</td>
                      <td className="px-3 py-1 text-right font-mono text-muted">
                        {(r.ratio * 100).toFixed(2)}%
                      </td>
                      <td className="px-3 py-1 text-right font-mono">{r.trades}</td>
                      <td className="px-3 py-1 text-right font-mono text-bull">{numText(r.buy, 0)}</td>
                      <td className="px-3 py-1 text-right font-mono text-bear">{numText(r.sell, 0)}</td>
                      <td className="px-3 py-1 text-right font-mono text-muted">
                        {(r.cum_ratio * 100).toFixed(1)}%
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>

          <div className="text-[11px] text-muted">
            量纲：成交量=手，成交额=元。累计占比自最低价档向上累加；POC 为全日成交最密集价，
            常被视为当日筹码重心。
          </div>
        </>
      ) : !isFetching ? (
        <EmptyState
          icon={BarChart3}
          title="暂无逐笔数据"
          hint="分价表由逐笔成交聚合而来，逐笔只保留最近一个交易日。确认代码正确、或等到交易时段再看。"
        />
      ) : null}
    </section>
  )
}

function OrderflowChart({ pts }: { pts: PulseOrderflow['points'] }) {
  const W = 900, H = 200, PL = 44, PR = 48, PT = 12, PB = 22
  const bw = (W - PL - PR) / Math.max(1, pts.length)
  const maxD = Math.max(1, ...pts.map((p) => Math.max(Math.abs(p.buy), Math.abs(p.sell))))
  const cumAbs = Math.max(1, ...pts.map((p) => Math.abs(p.cum_delta)))
  const y = (v: number) => PT + (1 - (v / maxD + 1) / 2) * (H - PT - PB)
  const yc = (v: number) => PT + (1 - (v / cumAbs + 1) / 2) * (H - PT - PB)
  const cumPath = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${(PL + i * bw + bw / 2).toFixed(1)},${yc(p.cum_delta).toFixed(1)}`).join(' ')
  const mid = PT + (H - PT - PB) / 2
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-[200px]">
      <line x1={PL} y1={mid} x2={W - PR} y2={mid} stroke="currentColor" className="text-border" strokeDasharray="3 3" />
      {pts.map((p, i) => {
        const cx = PL + i * bw + bw / 2
        const yb = y(p.buy), ys = y(p.sell)
        return (
          <g key={p.minute}>
            <rect x={cx - bw * 0.42} y={Math.min(yb, mid)} width={Math.max(1, bw * 0.4)} height={Math.abs(mid - yb)} className="fill-current text-bull" opacity={0.85} />
            <rect x={cx + bw * 0.02} y={ys} width={Math.max(1, bw * 0.4)} height={Math.abs(mid - ys)} className="fill-current text-bear" opacity={0.85} />
          </g>
        )
      })}
      <path d={cumPath} fill="none" className="text-accent" stroke="currentColor" strokeWidth={1.6} />
      <text x={2} y={PT + 10} className="fill-current text-muted" fontSize={10}>+{Math.round(maxD)}</text>
      <text x={2} y={H - PB} className="fill-current text-muted" fontSize={10}>-{Math.round(maxD)}</text>
      {pts.length > 0 && (
        <>
          <text x={PL} y={H - 6} className="fill-current text-muted" fontSize={10}>{pts[0].minute}</text>
          <text x={W - PR - 26} y={H - 6} className="fill-current text-muted" fontSize={10}>{pts[pts.length - 1].minute}</text>
        </>
      )}
    </svg>
  )
}

/** 足迹图用 canvas 画：格子数 = 价格档 × 时间桶，SVG 节点太多会卡。 */
function FootprintCanvas({ data }: { data: PulseFootprint }) {
  const ref = useRef<HTMLCanvasElement | null>(null)
  const rows = data.price_levels.length
  const cols = data.time_buckets.length

  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    const dpr = window.devicePixelRatio || 1
    const cssW = canvas.clientWidth || 900
    const cellH = 14
    const cssH = rows * cellH + 40
    canvas.width = cssW * dpr
    canvas.height = cssH * dpr
    canvas.style.height = `${cssH}px`
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, cssW, cssH)

    const style = getComputedStyle(canvas)
    const bull = style.getPropertyValue('--color-bull').trim() || '#ef4444'
    const bear = style.getPropertyValue('--color-bear').trim() || '#22c55e'
    const muted = style.getPropertyValue('--color-muted').trim() || '#888'

    const leftPad = 62, rightPad = 8, topPad = 16
    const cw = (cssW - leftPad - rightPad) / Math.max(1, cols)
    const maxVol = Math.max(1, ...data.cells.map((c) => c.volume))

    // 价格轴（纵，价格从高到低）
    ctx.font = '10px ui-sans-serif, system-ui'
    ctx.fillStyle = muted
    ctx.textAlign = 'right'
    for (let r = 0; r < rows; r++) {
      const price = data.price_levels[rows - 1 - r]
      const y = topPad + r * cellH
      if (r % Math.max(1, Math.ceil(rows / 12)) === 0) {
        ctx.fillText(price.toFixed(2), leftPad - 6, y + cellH - 3)
      }
    }

    // 网格
    const grid = new Map<string, (typeof data.cells)[number]>()
    for (const c of data.cells) grid.set(`${c.t}|${c.p}`, c)
    for (let ci = 0; ci < cols; ci++) {
      for (let r = 0; r < rows; r++) {
        const level = rows - 1 - r
        const cell = grid.get(`${data.time_buckets[ci]}|${level}`)
        if (!cell) continue
        const x = leftPad + ci * cw
        const y = topPad + r * cellH
        const alpha = 0.25 + 0.75 * Math.min(1, cell.volume / maxVol)
        // 一格内左右分色：左半主买、右半主卖，中间空格表示净差方向
        const total = cell.buy + cell.sell || 1
        ctx.globalAlpha = alpha
        ctx.fillStyle = cell.buy >= cell.sell ? bull : bear
        ctx.fillRect(x + 0.5, y + 0.5, Math.max(1, cw - 1), cellH - 1)
        ctx.globalAlpha = 1
        // 净差条：画在格子底部，宽度按净差占比
        const netW = (Math.abs(cell.delta) / total) * (cw - 2)
        ctx.fillStyle = cell.delta >= 0 ? bull : bear
        ctx.fillRect(x + 1, y + cellH - 3, Math.max(0, netW), 2)
      }
    }

    // 时间轴（横）
    ctx.fillStyle = muted
    ctx.textAlign = 'center'
    for (let ci = 0; ci < cols; ci++) {
      if (ci % Math.max(1, Math.ceil(cols / 12)) !== 0) continue
      ctx.fillText(data.time_buckets[ci], leftPad + ci * cw + cw / 2, cssH - 6)
    }

    // POC / VWAP 参考线
    const lineY = (price: number) => {
      const idx = rows - 1 - Math.round((price - data.low) / (data.step || 1))
      return topPad + Math.min(rows - 1, Math.max(0, idx)) * cellH + cellH / 2
    }
    if (data.poc != null) {
      ctx.strokeStyle = bull
      ctx.setLineDash([4, 3])
      ctx.beginPath(); ctx.moveTo(leftPad, lineY(data.poc)); ctx.lineTo(cssW - rightPad, lineY(data.poc)); ctx.stroke()
      ctx.setLineDash([])
    }
    if (data.vwap != null) {
      ctx.strokeStyle = muted
      ctx.beginPath(); ctx.moveTo(leftPad, lineY(data.vwap)); ctx.lineTo(cssW - rightPad, lineY(data.vwap)); ctx.stroke()
    }
  }, [data, rows, cols])

  return <canvas ref={ref} className="w-full rounded-card border border-border bg-surface" />
}

/* ------------------------------------------------------------------ 通用小件 */

function Metric({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <div className="rounded-card border border-border bg-surface px-3 py-2">
      <div className="text-[11px] text-muted">{label}</div>
      <div className={`text-sm font-semibold num tabular-nums ${cls ?? ''}`}>{value}</div>
    </div>
  )
}

function PartCell({ name, v, full, max }: { name: string; v: number | null; full: number; max: number }) {
  return (
    <div className="rounded-card border border-border bg-surface px-2.5 py-2">
      <div className="text-muted">{name}</div>
      <div className="flex items-center gap-2">
        <span className="num tabular-nums">{v == null ? '--' : numText(v, 2)}</span>
        <span className="text-muted">得分 {numText(full, 1)}/{max}</span>
      </div>
      <div className="h-1 mt-1 bg-surface-2 rounded-full overflow-hidden">
        <div className="h-full bg-accent" style={{ width: `${Math.min(100, (full / max) * 100)}%` }} />
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ 代码/名称搜索 */

function SymbolSearchInput({ value, onChange }: { value: string; onChange: (symbol: string) => void }) {
  const [query, setQuery] = useState(value)
  const [open, setOpen] = useState(false)
  const [debounced, setDebounced] = useState(value)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const t = setTimeout(() => setDebounced(query.trim()), 300)
    return () => clearTimeout(t)
  }, [query])

  useEffect(() => {
    setQuery(value)
    setDebounced(value)
  }, [value])

  const { data, isFetching } = useQuery({
    queryKey: [...QK.pulse, 'instrument-search', debounced],
    queryFn: () => api.instrumentSearch(debounced, 20, 'stock,etf'),
    enabled: debounced.length >= 2 && !/^\d{6}\.([A-Z]{2,3})$/.test(debounced),
    staleTime: 60_000,
  })

  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [])

  const select = (symbol: string) => {
    onChange(symbol)
    setQuery(symbol)
    setOpen(false)
  }

  const results = data?.results ?? []

  return (
    <div ref={ref} className="relative ml-auto">
      <label className="flex items-center gap-1.5 text-[11px] text-muted">
        代码 / 名称
        <input
          value={query}
          onChange={(e) => {
            setQuery(e.target.value)
            setOpen(true)
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              const raw = query.trim().toUpperCase()
              if (/^\d{6}\.([A-Z]{2,3})$/.test(raw)) {
                select(raw)
              } else if (results.length > 0) {
                select(results[0].symbol)
              }
            }
          }}
          placeholder="600519.SH 或 茅台"
          className="h-7 px-2 text-xs rounded-btn border border-border bg-surface text-foreground w-[150px]"
        />
      </label>
      {open && debounced.length >= 2 && (
        <div className="absolute right-0 top-full z-50 mt-1 w-[220px] max-h-[260px] overflow-y-auto rounded-card border border-border bg-surface shadow-lg">
          {isFetching && results.length === 0 ? (
            <div className="flex items-center gap-2 px-3 py-2 text-[11px] text-muted">
              <Loader2 className="h-3 w-3 animate-spin" /> 搜索中
            </div>
          ) : results.length === 0 ? (
            <div className="px-3 py-2 text-[11px] text-muted">无结果</div>
          ) : (
            results.map((r) => (
              <button
                key={r.symbol}
                onClick={() => select(r.symbol)}
                className="w-full text-left px-3 py-1.5 text-xs hover:bg-surface-2 border-b border-border last:border-0"
              >
                <span className="font-medium text-foreground">{r.name}</span>
                <span className="ml-2 text-muted">{r.symbol}</span>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  )
}

function SingleBlock({
  loading, empty, hint, children,
}: { loading: boolean; empty: boolean; hint: string; children: React.ReactNode }) {
  if (loading) {
    return (
      <div className="flex items-center gap-2 text-xs text-muted py-6">
        <Loader2 className="h-3.5 w-3.5 animate-spin" /> 加载中
      </div>
    )
  }
  if (empty) return <EmptyState icon={Activity} title="暂无数据" hint={hint} />
  return <>{children}</>
}
