import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Play, Square, Trophy, Sparkles } from 'lucide-react'
import { api, type StrategyDetail } from '@/lib/api'
import { fmtPct } from '@/lib/format'
import { EmptyState } from '@/components/EmptyState'
import { DatePicker } from '@/components/DatePicker'
import {
  startOptimize,
  stopOptimize,
  clearOptimize,
  tryReconnectOptimize,
  useOptimizerTask,
} from '@/lib/optimizerTask'
import { buildDefaultOverrides } from '@/lib/strategyOverrides'
import {
  INPUT_CLS,
  OBJECTIVES,
  GRID_MAX_COMBINATIONS,
  PARAM_SUGGESTION_KEY,
  type ParamSuggestionPayload,
  useParamSweep,
  StrategySelect,
  SweepParamList,
  CombosHint,
} from './components/paramSweep'

const TODAY = new Date().toISOString().slice(0, 10)
const ONE_YEAR_AGO = new Date(Date.now() - 365 * 864e5).toISOString().slice(0, 10)

export function StrategyOptimizer() {
  const task = useOptimizerTask()
  const { data: stratData } = useQuery({ queryKey: ['strategies'], queryFn: () => api.strategyList() })
  const strategies: StrategyDetail[] = stratData?.strategies ?? []

  // 切策略: 有任务在跑时先真正取消 (关 SSE + 后端 cancel + 清 localStorage), 不能静默丢
  const sweep = useParamSweep(strategies, () => {
    if (task?.isPending) stopOptimize()
    else clearOptimize()
  })
  const [objective, setObjective] = useState('sortino')
  const [start, setStart] = useState(ONE_YEAR_AGO)
  const [end, setEnd] = useState(TODAY)
  const [mode, setMode] = useState<'position' | 'full'>('position')
  /** 从「信号实验室」AI 归因带过来的建议(展示一次来源提示, 不参与后续渲染判断)。 */
  const [appliedFrom, setAppliedFrom] = useState<string | null>(null)
  const suggestionConsumed = useRef(false)

  // 刷新/切页后: 恢复未完成的优化任务
  useEffect(() => {
    tryReconnectOptimize()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 从 AI 归因跳转进来: 策略列表加载完后再套用建议, 且只消费一次(读完即删)。
  useEffect(() => {
    if (suggestionConsumed.current || !strategies.length) return
    const raw = sessionStorage.getItem(PARAM_SUGGESTION_KEY)
    if (!raw) return
    suggestionConsumed.current = true
    sessionStorage.removeItem(PARAM_SUGGESTION_KEY)
    try {
      const payload = JSON.parse(raw) as ParamSuggestionPayload
      if (sweep.applySuggestion(payload)) {
        setAppliedFrom(payload.source === 'explore'
          ? `AI 未给出可执行建议, 已按参数声明填入 ${payload.items.length} 个参数的探索网格`
          : `已按 AI 归因建议填入 ${payload.items.length} 个参数（持有 ${payload.horizon ?? '-'} 日口径）`)
      } else {
        setAppliedFrom(`AI 建议的策略「${payload.strategy_id}」不在当前策略列表中, 未套用`)
      }
    } catch {
      setAppliedFrom('AI 建议解析失败, 未套用')
    }
  }, [strategies, sweep])

  const canRun = sweep.strategyId && sweep.combos > 0 && sweep.combos <= GRID_MAX_COMBINATIONS
    && !sweep.gridError && !task?.isPending

  const onRun = () => {
    if (!canRun) return
    clearOptimize()
    startOptimize({
      strategy_id: sweep.strategyId,
      param_grid: sweep.buildGrid(),
      objective,
      // 未扫描参数固定为策略当前默认值; overrides 让 basic_filter/信号/风控按当前策略参与,
      // 保证优化的就是用户实际回测的策略 (而非被剥离配置的裸策略)。
      params: sweep.selected?.params_defaults,
      overrides: sweep.selected ? buildDefaultOverrides(sweep.selected) : undefined,
      start,
      end,
      mode,
    })
  }

  const result = task?.result
  const progress = task?.progress

  return (
    <div className="grid grid-cols-1 gap-3 lg:grid-cols-[320px_minmax(0,1fr)] h-full min-h-0 overflow-hidden">
      {/* ── 配置面板 ── */}
      <div className="space-y-3 rounded-card border border-border bg-surface p-4 overflow-y-auto min-h-0">
        <div>
          <label className="mb-1.5 block text-xs font-medium text-secondary">策略</label>
          <StrategySelect strategies={strategies} value={sweep.strategyId} onChange={sweep.selectStrategy} />
        </div>

        <div>
          <label className="mb-1.5 block text-xs font-medium text-secondary">优化目标</label>
          <select value={objective} onChange={e => setObjective(e.target.value)} className={INPUT_CLS}>
            {OBJECTIVES.map(o => <option key={o.id} value={o.id}>{o.label}</option>)}
          </select>
        </div>

        <div className="grid grid-cols-2 gap-2">
          <div>
            <label className="mb-1.5 block text-xs font-medium text-secondary">起始</label>
            <DatePicker value={start} onChange={setStart} />
          </div>
          <div>
            <label className="mb-1.5 block text-xs font-medium text-secondary">结束</label>
            <DatePicker value={end} onChange={setEnd} />
          </div>
        </div>

        <div>
          <label className="mb-1.5 block text-xs font-medium text-secondary">模式</label>
          <select value={mode} onChange={e => setMode(e.target.value as any)} className={INPUT_CLS}>
            <option value="position">组合仓位</option>
            <option value="full">全量独立</option>
          </select>
        </div>

        {appliedFrom && (
          <div className="flex items-start gap-1.5 rounded-input border border-accent/30 bg-accent/5 px-2.5 py-2 text-[11px] text-accent">
            <Sparkles className="mt-0.5 h-3 w-3 shrink-0" />
            <span>{appliedFrom}</span>
          </div>
        )}

        <SweepParamList params={sweep.params} sweeps={sweep.sweeps} updateSweep={sweep.updateSweep} />
        <CombosHint show={!!sweep.strategyId} combos={sweep.combos} gridError={sweep.gridError} />

        {task?.isPending ? (
          <button onClick={stopOptimize} className="inline-flex w-full items-center justify-center gap-1.5 rounded-btn bg-red-500/90 px-3 py-2 text-xs font-medium text-white hover:bg-red-500">
            <Square className="h-3.5 w-3.5" /> 停止
          </button>
        ) : (
          <button onClick={onRun} disabled={!canRun} className="inline-flex w-full items-center justify-center gap-1.5 rounded-btn bg-accent px-3 py-2 text-xs font-medium text-white hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed">
            <Play className="h-3.5 w-3.5" /> 开始优化
          </button>
        )}
      </div>

      {/* ── 结果面板 ── */}
      <div className="min-h-0 rounded-card border border-border bg-surface p-4 overflow-y-auto">
        {task?.error && (
          <div className="mb-3 rounded-input border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-400">{task.error}</div>
        )}

        {task?.isPending && progress && (
          <div className="mb-4">
            <div className="mb-1 flex justify-between text-xs text-secondary">
              <span>进度 {progress.done}/{progress.total}</span>
              <span>当前最优: {progress.best_score != null ? progress.best_score.toFixed(3) : '—'}</span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-elevated">
              <div className="h-full bg-accent transition-all" style={{ width: `${progress.total ? (progress.done / progress.total) * 100 : 0}%` }} />
            </div>
          </div>
        )}

        {!result && !task?.isPending && (
          <EmptyState title="参数优化" hint="选择策略、勾选要扫描的参数与优化目标；任务会在独立 worker 中复用基础数据并串行执行组合。" />
        )}

        {result && (
          <div className="space-y-4">
            {result.best_params && (
              <div className="rounded-card border border-accent/30 bg-accent/5 p-3">
                <div className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold text-accent">
                  <Trophy className="h-3.5 w-3.5" /> 最优参数 · {result.objective} = {result.best_score}
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {Object.entries(result.best_params).map(([k, v]) => (
                    <span key={k} className="rounded-full border border-border bg-surface px-2 py-0.5 text-[11px]">{k}: {String(v)}</span>
                  ))}
                </div>
              </div>
            )}

            <div className="text-xs text-secondary">
              {result.n_completed}/{result.n_combinations} 组完成 · 耗时 {(result.elapsed_ms / 1000).toFixed(1)}s
            </div>

            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-border text-secondary">
                    <th className="px-2 py-1.5 text-left">#</th>
                    <th className="px-2 py-1.5 text-left">参数</th>
                    <th className="px-2 py-1.5 text-right">{result.objective}</th>
                    <th className="px-2 py-1.5 text-right">夏普</th>
                    <th className="px-2 py-1.5 text-right">索提诺</th>
                    <th className="px-2 py-1.5 text-right">总收益</th>
                    <th className="px-2 py-1.5 text-right">最大回撤</th>
                    <th className="px-2 py-1.5 text-right">胜率</th>
                    <th className="px-2 py-1.5 text-right">交易数</th>
                  </tr>
                </thead>
                <tbody>
                  {result.results.slice(0, 50).map(r => (
                    <tr key={r.rank} className="border-b border-border/40 hover:bg-elevated/50">
                      <td className="px-2 py-1.5 text-secondary">{r.rank}</td>
                      <td className="px-2 py-1.5">
                        {r.error
                          ? <span className="text-red-400">失败: {r.error.slice(0, 40)}</span>
                          : <span className="text-foreground">{Object.entries(r.params).map(([k, v]) => `${k}=${v}`).join(', ')}</span>}
                      </td>
                      <td className="px-2 py-1.5 text-right font-medium">{r.objective_raw != null ? r.objective_raw.toFixed(3) : '—'}</td>
                      <td className="px-2 py-1.5 text-right">{r.stats?.sharpe ?? '—'}</td>
                      <td className="px-2 py-1.5 text-right">{r.stats?.sortino ?? '—'}</td>
                      <td className="px-2 py-1.5 text-right">{r.stats?.total_return != null ? fmtPct(r.stats.total_return) : '—'}</td>
                      <td className="px-2 py-1.5 text-right">{r.stats?.max_drawdown != null ? fmtPct(r.stats.max_drawdown) : '—'}</td>
                      <td className="px-2 py-1.5 text-right">{r.stats?.win_rate != null ? fmtPct(r.stats.win_rate) : '—'}</td>
                      <td className="px-2 py-1.5 text-right">{r.stats?.n_trades ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {result.results.length > 50 && (
                <div className="mt-2 text-center text-[11px] text-secondary">
                  仅显示前 50 组 · 共 {result.results.length} 组
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
