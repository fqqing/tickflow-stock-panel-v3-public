/**
 * 信号实验室（Signal Lab）。
 *
 * 与「回测」页的分工：回测回答「这套资金规则能赚多少」，只统计实际成交、
 * 受持仓名额和评分排序截断的那部分信号；这里回答「这些信号本身后来怎么走」——
 * 把策略在区间内产生的**每一条**信号都摊平，样本量大一个量级，可以反复用
 * 不同持有期、不同分组复盘而不必重算信号。
 *
 * 三个层次（对应后端三个端点）：
 * 1. 战绩汇总 summary — 胜率 / 期望收益 / 盈亏比 / 平均最大浮盈浮亏
 * 2. 形态归因 attribution — 把信号按特征分桶，看哪个档位更好（这才是可调参数的依据）
 * 3. 信号台账 outcomes — 每条信号的成交价与各持有期收益，用来人工复核上面两个结论
 *
 * ⚠️ 归因只暴露 ctx_*（信号当时已知的形态）与 entry_signal_name（信号分支）。
 *    mfe/mae/ret_* 都含未来信息，拿它们当特征会得到必然赚钱的假结论。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { FlaskConical, Play, RefreshCw, Loader2, Info, Sparkles, Target, SlidersHorizontal } from 'lucide-react'
import {
  api,
  type SignalLabAttributionRow,
  type SignalLabDataset,
  type SignalLabScoreRow,
  type SignalLabStrategy,
  type SignalLabSuggestions,
} from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import {
  PARAM_SUGGESTION_KEY,
  type ParamSuggestionPayload,
} from '@/pages/backtest/components/paramSweep'

/** 默认观察持有期（交易日），与后端 DEFAULT_HORIZONS 一致。 */
const DEFAULT_HORIZONS = '1,3,5,10,20,60'
/** 小样本试算的标的数量档位：全市场跑一次要几分钟，先小样本看结论。 */
const LIMIT_OPTIONS = [50, 100, 300, 1000]

type HorizonKey = { n: number; nKey: string; winKey: string; meanKey: string; pfKey: string }

/** 汇总列是 ret{horizon}_*（不是台账的 ret_{horizon}d），这里统一拼接。 */
function horizonKeys(horizons: number[]): HorizonKey[] {
  return horizons.map((n) => ({
    n,
    nKey: `ret${n}_n`,
    winKey: `ret${n}_win_rate`,
    meanKey: `ret${n}_mean`,
    pfKey: `ret${n}_profit_factor`,
  }))
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

export function SignalLab() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [strategyId, setStrategyId] = useState('')
  const [horizons, setHorizons] = useState(DEFAULT_HORIZONS)
  const [limit, setLimit] = useState(100)
  const [runId, setRunId] = useState<string | null>(null)
  const [selected, setSelected] = useState<{ start?: string; end?: string }>({})
  const [page, setPage] = useState(0)

  // AI 形态归因解读(流式)
  const [insight, setInsight] = useState('')
  const [insightError, setInsightError] = useState('')
  const [insightOn, setInsightOn] = useState(false)
  // AI 正文末尾附的机器可读参数建议(后端已按参数声明收口), 可一键送进网格搜索
  const [suggestions, setSuggestions] = useState<SignalLabSuggestions | null>(null)
  const [focus, setFocus] = useState('')
  const insightAbort = useRef<AbortController | null>(null)

  // 当日候选打分(选股 + 分档打分, 较重, 显式触发)
  const [scoreOn, setScoreOn] = useState(false)

  /** 分支对比的分组列：只暴露信号分支（信号当时已知），不开放 mfe/mae 等未来量。 */
  const groupBy = 'entry_signal_name'

  const { data: strategiesData } = useQuery({
    queryKey: [...QK.signalLab, 'strategies'],
    queryFn: () => api.signalLabStrategies(),
    staleTime: 5 * 60_000,
  })

  const strategies = useMemo(
    () => (strategiesData?.strategies ?? []).filter((s: SignalLabStrategy) => s.supported),
    [strategiesData],
  )
  const contextFeatures = strategiesData?.context_features ?? []

  // 首次拿到策略列表时默认选中第一个，避免空页面
  useEffect(() => {
    if (!strategyId && strategies.length > 0) setStrategyId(strategies[0].id)
  }, [strategies, strategyId])

  const { data: datasetsData, refetch: refetchDatasets } = useQuery({
    queryKey: [...QK.signalLab, 'datasets', strategyId],
    queryFn: () => api.signalLabDatasets(strategyId || undefined),
    enabled: !!strategyId,
    staleTime: 30_000,
  })

  const datasets = useMemo(() => datasetsData?.datasets ?? [], [datasetsData])
  const dataset: SignalLabDataset | undefined = useMemo(() => {
    if (!datasets.length) return undefined
    if (selected.start && selected.end) {
      const hit = datasets.find((d) => d.start === selected.start && d.end === selected.end)
      if (hit) return hit
    }
    return datasets[0]
  }, [datasets, selected])

  const range = dataset ? { start: dataset.start ?? undefined, end: dataset.end ?? undefined } : {}

  // ===== 复盘任务 =====
  const startRun = useCallback(async () => {
    if (!strategyId) return
    const parsed = horizons.split(',').map((s) => parseInt(s.trim(), 10)).filter((n) => n > 0)
    const res = await api.signalLabRun({
      strategy_id: strategyId,
      horizons: parsed.length ? parsed : [5, 20],
      limit,
    })
    setRunId(res.run_id)
    setSelected({})
  }, [strategyId, horizons, limit])

  // 轮询中的任务：2 秒一次，终态停止
  const { data: run } = useQuery({
    queryKey: [...QK.signalLab, 'run', runId],
    queryFn: () => api.signalLabRunStatus(runId as string),
    enabled: !!runId,
    refetchInterval: (query) => {
      const status = (query.state.data as { status?: string } | undefined)?.status
      return status === 'succeeded' || status === 'failed' ? false : 2000
    },
  })

  const running = run?.status === 'pending' || run?.status === 'running'
  useEffect(() => {
    if (run?.status === 'succeeded') {
      void refetchDatasets()
      void qc.invalidateQueries({ queryKey: [...QK.signalLab] })
    }
  }, [run?.status, refetchDatasets, qc])

  // ===== 查询 =====
  const summaryKey = [QK.signalLab, 'summary', strategyId, dataset?.start, dataset?.end] as const
  const { data: summary } = useQuery({
    queryKey: summaryKey,
    queryFn: () => api.signalLabSummary({ strategy_id: strategyId, ...range }),
    enabled: !!strategyId && !!dataset,
  })

  const { data: attribution } = useQuery({
    queryKey: [QK.signalLab, 'attribution', strategyId, dataset?.start, dataset?.end, summary?.horizons] as const,
    queryFn: () =>
      api.signalLabAttribution({
        strategy_id: strategyId,
        ...range,
        horizon: summary?.horizons?.length ? summary.horizons[summary.horizons.length - 1] : undefined,
        min_samples: 10,
      }),
    enabled: !!strategyId && !!dataset,
  })

  const { data: outcomes } = useQuery({
    queryKey: [QK.signalLab, 'outcomes', strategyId, dataset?.start, dataset?.end, page] as const,
    queryFn: () =>
      api.signalLabOutcomes({ strategy_id: strategyId, ...range, limit: 100, offset: page * 100 }),
    enabled: !!strategyId && !!dataset,
  })

  const { data: scoreToday, isFetching: scoreLoading } = useQuery({
    queryKey: [QK.signalLab, 'score-today', strategyId, dataset?.start, dataset?.end] as const,
    queryFn: () => api.signalLabScoreToday({ strategy_id: strategyId, ...range, limit: 30, min_samples: 10 }),
    enabled: !!strategyId && !!dataset && scoreOn,
    staleTime: 5 * 60_000,
  })

  const runInsight = useCallback(async () => {
    if (!strategyId) return
    insightAbort.current?.abort()
    const controller = new AbortController()
    insightAbort.current = controller
    setInsightOn(true)
    setInsight('')
    setInsightError('')
    setSuggestions(null)
    const horizon = summary?.horizons?.length ? summary.horizons[summary.horizons.length - 1] : undefined
    try {
      await api.signalLabInsight(
        { strategy_id: strategyId, ...range, horizon, min_samples: 10, focus: focus || undefined },
        (ev) => {
          if (ev.type === 'delta') setInsight((prev) => prev + (ev.content ?? ''))
          else if (ev.type === 'error') setInsightError(String(ev.message ?? 'AI 解读失败'))
          else if (ev.type === 'suggestions') {
            setSuggestions({
              strategy_id: ev.strategy_id,
              horizon: ev.horizon,
              source: ev.source,
              items: ev.items,
              combos: ev.combos,
            })
          }
        },
        controller.signal,
      )
    } catch (e) {
      if (!controller.signal.aborted) setInsightError(e instanceof Error ? e.message : String(e))
    } finally {
      setInsightOn(false)
    }
  }, [strategyId, range, summary?.horizons, focus])

  /** 把 AI 给的参数建议交给「回测 / 参数优化」页(一次性, 读完即删)。 */
  const sendSuggestionsToOptimizer = useCallback(() => {
    if (!suggestions?.items?.length) return
    const payload: ParamSuggestionPayload = {
      strategy_id: suggestions.strategy_id || strategyId,
      horizon: suggestions.horizon,
      source: suggestions.source,
      items: suggestions.items,
      combos: suggestions.combos,
    }
    sessionStorage.setItem(PARAM_SUGGESTION_KEY, JSON.stringify(payload))
    navigate('/backtest?tab=robustness')
  }, [suggestions, strategyId, navigate])

  const keys = horizonKeys(summary?.horizons ?? [])
  const overall = summary?.overall ?? {}
  const hasDataset = !!dataset

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <PageHeader
        title="信号实验室"
        subtitle={dataset ? `${dataset.strategy_id} · ${dataset.start} ~ ${dataset.end} · ${dataset.rows ?? 0} 条` : '回答「这些信号本身后来怎么走」'}
        right={
          <div className="flex items-center gap-2">
            <button
              onClick={() => void refetchDatasets()}
              className="flex items-center gap-1 px-2 py-1 text-xs rounded-btn border border-border text-secondary hover:text-foreground hover:bg-elevated cursor-pointer"
            >
              <RefreshCw className="h-3 w-3" /> 刷新
            </button>
          </div>
        }
      />

      {/* 控制条 */}
      <div className="flex flex-wrap items-end gap-3 px-5 py-3 border-b border-border bg-elevated/20">
        <label className="flex flex-col gap-1 text-[11px] text-muted">
          策略
          <select
            value={strategyId}
            onChange={(e) => { setStrategyId(e.target.value); setSelected({}); setPage(0) }}
            className="h-7 px-2 text-xs rounded-btn border border-border bg-surface text-foreground min-w-[160px]"
          >
            {strategies.map((s) => (
              <option key={s.id} value={s.id}>{s.name || s.id}</option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-[11px] text-muted">
          数据集
          <select
            value={dataset ? `${dataset.start}~${dataset.end}` : ''}
            onChange={(e) => {
              const [start, end] = e.target.value.split('~')
              setSelected({ start, end })
              setPage(0)
            }}
            className="h-7 px-2 text-xs rounded-btn border border-border bg-surface text-foreground min-w-[200px]"
          >
            {datasets.map((d) => (
              <option key={d.path} value={`${d.start}~${d.end}`}>
                {d.start} ~ {d.end}（{d.rows ?? 0} 条）
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-[11px] text-muted">
          持有期(交易日)
          <input
            value={horizons}
            onChange={(e) => setHorizons(e.target.value)}
            className="h-7 px-2 text-xs rounded-btn border border-border bg-surface text-foreground w-[140px]"
          />
        </label>

        <label className="flex flex-col gap-1 text-[11px] text-muted">
          标的数
          <select
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
            className="h-7 px-2 text-xs rounded-btn border border-border bg-surface text-foreground"
          >
            {LIMIT_OPTIONS.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>

        <button
          onClick={() => void startRun()}
          disabled={!strategyId || running}
          className="flex items-center gap-1.5 h-7 px-3 text-xs rounded-btn bg-accent text-white hover:opacity-90 disabled:opacity-50 cursor-pointer"
        >
          {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
          {running ? `复盘中 ${run?.progress ?? 0}%` : '跑一次复盘'}
        </button>

        {run?.status === 'failed' && (
          <span className="text-xs text-danger">失败：{run.error}</span>
        )}
        {running && run?.log?.length ? (
          <span className="text-[11px] text-muted">{run.log[run.log.length - 1].msg}</span>
        ) : null}
      </div>

      <div className="flex-1 overflow-y-auto px-5 py-4 space-y-5">
        {!hasDataset && !running && (
          <EmptyState
            icon={FlaskConical}
            title="还没有信号台账"
            hint="选好策略后点「跑一次复盘」，系统会算出该策略在区间内的全部信号，并记录每条信号之后 1/3/5/10/20/60 个交易日的表现。首次建议用 100 只标的试算（约 1 分钟）。"
          />
        )}

        {/* 1. 战绩汇总 */}
        {summary && (
          <section>
            <h2 className="text-sm font-semibold mb-2">
              战绩汇总
              <span className="ml-2 text-[11px] font-normal text-muted">
                共 {summary.n_signals} 条信号 · 成交 {numText(overall.n_filled, 0)} · 前瞻不足 {numText(overall.n_truncated, 0)}
              </span>
            </h2>
            <div className="overflow-x-auto rounded-btn border border-border">
              <table className="w-full text-xs border-collapse">
                <thead>
                  <tr className="bg-elevated/40 text-muted">
                    <th className="px-3 py-2 text-left font-medium">持有期</th>
                    <th className="px-3 py-2 text-right font-medium">样本</th>
                    <th className="px-3 py-2 text-right font-medium">胜率</th>
                    <th className="px-3 py-2 text-right font-medium">平均收益</th>
                    <th className="px-3 py-2 text-right font-medium">中位收益</th>
                    <th className="px-3 py-2 text-right font-medium">盈亏比</th>
                    <th className="px-3 py-2 text-right font-medium">超额(对全市场)</th>
                  </tr>
                </thead>
                <tbody>
                  {keys.map((k) => (
                    <tr key={k.n} className="border-t border-border/60">
                      <td className="px-3 py-1.5 text-secondary">{k.n} 日</td>
                      <td className="px-3 py-1.5 text-right num tabular-nums text-muted">{numText(overall[k.nKey], 0)}</td>
                      <td className="px-3 py-1.5 text-right num tabular-nums">{pctText(overall[k.winKey], 1)}</td>
                      <td className={`px-3 py-1.5 text-right num tabular-nums ${pctClass(overall[k.meanKey])}`}>{pctText(overall[k.meanKey])}</td>
                      <td className={`px-3 py-1.5 text-right num tabular-nums ${pctClass(overall[`ret${k.n}_median`])}`}>{pctText(overall[`ret${k.n}_median`])}</td>
                      <td className="px-3 py-1.5 text-right num tabular-nums">{numText(overall[k.pfKey])}</td>
                      <td className={`px-3 py-1.5 text-right num tabular-nums ${pctClass(overall[`exc${k.n}_mean`])}`}>{pctText(overall[`exc${k.n}_mean`])}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="mt-1.5 text-[11px] text-muted flex items-center gap-1">
              <Info className="h-3 w-3" />
              收益是 T+1 开盘成交口径，null（停牌 / 前瞻窗口不足）不进分母也不按 0 补齐；
              盈亏比 = 盈利总和 ÷ 亏损绝对值总和。
            </p>
          </section>
        )}

        {/* 2. 形态归因 */}
        {attribution && attribution.rows.length > 0 && (
          <section>
            <div className="flex flex-wrap items-center gap-2 mb-2">
              <h2 className="text-sm font-semibold">
                形态归因
                <span className="ml-2 text-[11px] font-normal text-muted">
                  按 {attribution.horizon} 日收益排序 · 特征只取信号当时已知的量
                  {contextFeatures.length > 0 ? `（${contextFeatures.join(' / ')}）` : ''}
                </span>
              </h2>
              <div className="ml-auto flex items-center gap-2">
                <input
                  value={focus}
                  onChange={(e) => setFocus(e.target.value)}
                  placeholder="追加关注点(可选)"
                  className="h-7 px-2 text-xs rounded-btn border border-border bg-surface text-foreground w-[160px]"
                />
                <button
                  onClick={() => void runInsight()}
                  disabled={insightOn}
                  className="flex items-center gap-1 px-2 py-1 text-xs rounded-btn border border-border text-secondary hover:text-foreground hover:bg-elevated disabled:opacity-50 cursor-pointer"
                >
                  {insightOn ? <Loader2 className="h-3 w-3 animate-spin" /> : <Sparkles className="h-3 w-3" />}
                  {insightOn ? '解读中' : 'AI 解读'}
                </button>
              </div>
            </div>
            {(insight || insightError) && (
              <div className="mb-3 rounded-btn border border-border bg-elevated/20 p-3">
                <div className="text-[11px] text-muted mb-1 flex items-center gap-1">
                  <Sparkles className="h-3 w-3" /> AI 归因解读（事实由后端统计, 模型只做解读）
                </div>
                {insightError ? (
                  <p className="text-xs text-danger whitespace-pre-wrap">{insightError}</p>
                ) : (
                  <p className="text-xs text-secondary whitespace-pre-wrap leading-relaxed">
                    {insight}
                    {insightOn ? <span className="ml-0.5 animate-pulse">▍</span> : null}
                  </p>
                )}
              </div>
            )}
            {suggestions && suggestions.items.length > 0 && (
              <div className="mb-3 rounded-btn border border-accent/30 bg-accent/5 p-3">
                <div className="mb-2 flex items-center gap-1.5 text-[11px] text-accent">
                  <SlidersHorizontal className="h-3 w-3" />
                  {suggestions.source === 'explore'
                    ? '探索网格（AI 未给出可执行建议 · 按参数声明等距铺开）'
                    : '参数建议（已按参数声明收口）'}
                  <span className="text-muted">共 {suggestions.combos} 组组合</span>
                  <button
                    onClick={sendSuggestionsToOptimizer}
                    className="ml-auto flex items-center gap-1 px-2 py-0.5 text-[11px] rounded-btn border border-accent/40 text-accent hover:bg-accent/10 cursor-pointer"
                  >
                    <Play className="h-3 w-3" /> 填入网格搜索
                  </button>
                </div>
                <div className="space-y-1.5">
                  {suggestions.items.map((it) => (
                    <div key={it.param_id} className="flex flex-wrap items-baseline gap-x-2 text-xs">
                      <span className="font-medium text-foreground">{it.label}</span>
                      <span className="text-muted">({it.param_id})</span>
                      <span className="text-secondary">
                        {it.direction === 'up' ? '建议上调' : it.direction === 'down' ? '建议下调' : '建议附近微调'}
                      </span>
                      <span className="num tabular-nums text-secondary">
                        {typeof it.grid === 'object' && !Array.isArray(it.grid) && 'min' in it.grid
                          ? `${it.grid.min} ~ ${it.grid.max} / step ${it.grid.step}`
                          : Array.isArray(it.grid) ? `候选 ${it.grid.join(' / ')}` : ''}
                      </span>
                      {it.reason && <span className="text-muted">· {it.reason}</span>}
                    </div>
                  ))}
                </div>
                <div className="mt-2 text-[11px] text-muted">
                  填入后只勾选这些参数, 其余参数保持默认; 起止区间与目标请在优化页自行确认。
                </div>
              </div>
            )}
            <div className="overflow-x-auto rounded-btn border border-border">
              <table className="w-full text-xs border-collapse">
                <thead>
                  <tr className="bg-elevated/40 text-muted">
                    <th className="px-3 py-2 text-left font-medium">特征</th>
                    <th className="px-3 py-2 text-left font-medium">档位</th>
                    <th className="px-3 py-2 text-right font-medium">样本</th>
                    <th className="px-3 py-2 text-right font-medium">胜率</th>
                    <th className="px-3 py-2 text-right font-medium">平均收益</th>
                    <th className="px-3 py-2 text-right font-medium">盈亏比</th>
                  </tr>
                </thead>
                <tbody>
                  {attribution.rows.map((r: SignalLabAttributionRow, i: number) => (
                    <tr key={`${r.feature}-${r.bucket}-${i}`} className="border-t border-border/60 hover:bg-elevated/30">
                      <td className="px-3 py-1.5 text-secondary">{r.feature}</td>
                      <td className="px-3 py-1.5">{r.bucket}</td>
                      <td className="px-3 py-1.5 text-right num tabular-nums text-muted">{r.ret_n}</td>
                      <td className="px-3 py-1.5 text-right num tabular-nums">{pctText(r.ret_win_rate, 1)}</td>
                      <td className={`px-3 py-1.5 text-right num tabular-nums ${pctClass(r.ret_mean)}`}>{pctText(r.ret_mean)}</td>
                      <td className="px-3 py-1.5 text-right num tabular-nums">{numText(r.ret_profit_factor)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {attribution.requested_horizon != null && attribution.requested_horizon !== attribution.horizon && (
              <p className="mt-1.5 text-[11px] text-warning">
                台账里没有 {attribution.requested_horizon} 日的收益列，已回退到 {attribution.horizon} 日。
              </p>
            )}
          </section>
        )}

        {/* 2.5 当日候选打分 */}
        <section>
          <div className="flex flex-wrap items-center gap-2 mb-2">
            <h2 className="text-sm font-semibold">
              当日候选打分
              <span className="ml-2 text-[11px] font-normal text-muted">
                把归因学到的档位搬到今天 · 只改排序, 不改选股结果
              </span>
            </h2>
            <button
              onClick={() => setScoreOn(true)}
              disabled={!strategyId || !dataset || scoreOn}
              className="ml-auto flex items-center gap-1 px-2 py-1 text-xs rounded-btn border border-border text-secondary hover:text-foreground hover:bg-elevated disabled:opacity-50 cursor-pointer"
            >
              {scoreLoading ? <Loader2 className="h-3 w-3 animate-spin" /> : <Target className="h-3 w-3" />}
              {scoreOn ? '重新打分' : '算今日候选'}
            </button>
          </div>
          {scoreToday && (
            <>
              <p className="mb-2 text-[11px] text-muted flex items-center gap-1">
                <Info className="h-3 w-3" />
                {scoreToday.as_of} 选出 {scoreToday.n_candidates} 只 · 打分 {scoreToday.n_scored} 只 ·
                按 {scoreToday.horizon} 日历史表现加权（样本量加权平均）
                {scoreToday.notes.length > 0 ? ` · ${scoreToday.notes.join('；')}` : ''}
              </p>
              {scoreToday.rows.length > 0 && (
                <div className="overflow-x-auto rounded-btn border border-border">
                  <table className="w-full text-xs border-collapse">
                    <thead>
                      <tr className="bg-elevated/40 text-muted">
                        <th className="px-3 py-2 text-left font-medium">#</th>
                        <th className="px-3 py-2 text-left font-medium">代码</th>
                        <th className="px-3 py-2 text-left font-medium">名称</th>
                        <th className="px-3 py-2 text-right font-medium">现价</th>
                        <th className="px-3 py-2 text-right font-medium">涨跌</th>
                        <th className="px-3 py-2 text-right font-medium">形态分</th>
                        <th className="px-3 py-2 text-left font-medium">命中档位(历史表现)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {scoreToday.rows.map((r: SignalLabScoreRow, i: number) => (
                        <tr key={r.symbol} className="border-t border-border/60 hover:bg-elevated/30">
                          <td className="px-3 py-1.5 text-muted">{i + 1}</td>
                          <td className="px-3 py-1.5 font-mono">
                            <Link to={`/stock/${encodeURIComponent(r.symbol)}`} className="text-accent hover:underline">
                              {r.symbol}
                            </Link>
                          </td>
                          <td className="px-3 py-1.5">{r.name ?? '--'}</td>
                          <td className="px-3 py-1.5 text-right num tabular-nums">{numText(r.close)}</td>
                          <td className={`px-3 py-1.5 text-right num tabular-nums ${pctClass(r.change_pct)}`}>
                            {pctText(r.change_pct)}
                          </td>
                          <td className={`px-3 py-1.5 text-right num tabular-nums ${pctClass(r.score)}`}>
                            {pctText(r.score)}
                          </td>
                          <td className="px-3 py-1.5 text-[11px] text-muted">
                            {r.reasons.map((x) => `${x.label}=${x.bucket}(${pctText(x.mean)}, n=${x.n})`).join(' · ') || '--'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </section>

        {/* 3. 分组汇总（按信号分支） */}
        {summary && summary.groups.length > 0 && (
          <section>
            <h2 className="text-sm font-semibold mb-2">
              分支对比
              <span className="ml-2 text-[11px] font-normal text-muted">按 {groupBy} 分组</span>
            </h2>
            <div className="overflow-x-auto rounded-btn border border-border">
              <table className="w-full text-xs border-collapse">
                <thead>
                  <tr className="bg-elevated/40 text-muted">
                    <th className="px-3 py-2 text-left font-medium">{groupBy}</th>
                    <th className="px-3 py-2 text-right font-medium">信号数</th>
                    {keys.map((k) => (
                      <th key={k.n} className="px-3 py-2 text-right font-medium">{k.n}日胜率</th>
                    ))}
                    <th className="px-3 py-2 text-right font-medium">{keys.length ? `${keys[keys.length - 1].n}日均值` : '均值'}</th>
                  </tr>
                </thead>
                <tbody>
                  {summary.groups.map((g: Record<string, unknown>, i: number) => (
                    <tr key={String(g[groupBy] ?? i)} className="border-t border-border/60">
                      <td className="px-3 py-1.5 text-secondary">{String(g[groupBy] ?? '--')}</td>
                      <td className="px-3 py-1.5 text-right num tabular-nums text-muted">{numText(g.n_signals, 0)}</td>
                      {keys.map((k) => (
                        <td key={k.n} className="px-3 py-1.5 text-right num tabular-nums">{pctText(g[k.winKey], 1)}</td>
                      ))}
                      <td className={`px-3 py-1.5 text-right num tabular-nums ${pctClass(g[keys.length ? keys[keys.length - 1].meanKey : ''])}`}>
                        {pctText(g[keys.length ? keys[keys.length - 1].meanKey : ''])}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}

        {/* 4. 信号台账 */}
        {outcomes && outcomes.rows.length > 0 && (
          <section>
            <h2 className="text-sm font-semibold mb-2">
              信号台账
              <span className="ml-2 text-[11px] font-normal text-muted">
                共 {outcomes.total} 条 · 第 {page + 1} 页
              </span>
            </h2>
            <div className="overflow-x-auto rounded-btn border border-border">
              <table className="w-full text-xs border-collapse">
                <thead>
                  <tr className="bg-elevated/40 text-muted">
                    <th className="px-3 py-2 text-left font-medium">代码</th>
                    <th className="px-3 py-2 text-left font-medium">名称</th>
                    <th className="px-3 py-2 text-left font-medium">信号日</th>
                    <th className="px-3 py-2 text-left font-medium">成交日</th>
                    <th className="px-3 py-2 text-right font-medium">成交价</th>
                    <th className="px-3 py-2 text-left font-medium">信号</th>
                    {summary?.horizons?.map((h) => (
                      <th key={h} className="px-3 py-2 text-right font-medium">{h}日</th>
                    ))}
                    <th className="px-3 py-2 text-right font-medium">最大浮盈</th>
                    <th className="px-3 py-2 text-right font-medium">最大浮亏</th>
                  </tr>
                </thead>
                <tbody>
                  {outcomes.rows.map((r: Record<string, unknown>, i: number) => (
                    <tr key={`${String(r.symbol)}-${String(r.signal_date)}-${i}`} className="border-t border-border/60 hover:bg-elevated/30">
                      <td className="px-3 py-1.5 font-mono">
                        <Link to={`/stock/${encodeURIComponent(String(r.symbol))}`} className="text-accent hover:underline">
                          {String(r.symbol)}
                        </Link>
                      </td>
                      <td className="px-3 py-1.5">{String(r.name ?? '--')}</td>
                      <td className="px-3 py-1.5 text-muted">{String(r.signal_date)}</td>
                      <td className="px-3 py-1.5 text-muted">{String(r.fill_date ?? '--')}</td>
                      <td className="px-3 py-1.5 text-right num tabular-nums">{numText(r.entry_price)}</td>
                      <td className="px-3 py-1.5">{String(r.entry_signal_name ?? '--')}</td>
                      {summary?.horizons?.map((h) => (
                        <td key={h} className={`px-3 py-1.5 text-right num tabular-nums ${pctClass(r[`ret_${h}d`])}`}>
                          {pctText(r[`ret_${h}d`])}
                        </td>
                      ))}
                      <td className="px-3 py-1.5 text-right num tabular-nums text-bull">{pctText(r.mfe)}</td>
                      <td className="px-3 py-1.5 text-right num tabular-nums text-bear">{pctText(r.mae)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="mt-2 flex items-center gap-2">
              <button
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                disabled={page === 0}
                className="px-2 py-1 text-xs rounded-btn border border-border disabled:opacity-40 cursor-pointer"
              >上一页</button>
              <button
                onClick={() => setPage((p) => p + 1)}
                disabled={outcomes.rows.length < 100}
                className="px-2 py-1 text-xs rounded-btn border border-border disabled:opacity-40 cursor-pointer"
              >下一页</button>
            </div>
          </section>
        )}
      </div>
    </div>
  )
}
