import { useCallback, useEffect, useMemo, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { X, Send, Loader2, CheckCircle2, AlertTriangle } from 'lucide-react'
import { api, type LarkPushResult, type LarkTableOption } from '@/lib/api'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'

interface Props {
  /** 页面当前日期, 用作默认值 */
  asOf: string
  minDate?: string
  maxDate?: string
  market?: string
  /** 策略池的策略 id -> 中文名, 用于把表 id 显示成面板里的名字 */
  presetNames?: Record<string, string>
  onClose: () => void
}

/**
 * 推送飞书对话框 —— 选目标 + 选日期, 显式触发。
 *
 * 取代原先「计划任务定时跑脚本」的方式: 脚本里策略/日期写死, 换一次改一次代码,
 * 而且跑没跑、推了几条只能翻日志。这里结果直接回显。
 */
export function LarkPushDialog({ asOf, minDate, maxDate, market = 'cn', presetNames, onClose }: Props) {
  const [tables, setTables] = useState<LarkTableOption[]>([])
  const [loading, setLoading] = useState(true)
  const [cliAvailable, setCliAvailable] = useState<boolean | null>(null)
  const [cliPath, setCliPath] = useState('')

  const [target, setTarget] = useState('')
  const [date, setDate] = useState(asOf || '')
  const [dryRun, setDryRun] = useState(false)
  const [force, setForce] = useState(false)
  const [withMomentum, setWithMomentum] = useState(true)
  const [pushing, setPushing] = useState(false)
  const [result, setResult] = useState<LarkPushResult | null>(null)
  const [error, setError] = useState('')

  const backdrop = useDialogBackdrop(onClose, () => !pushing)

  useEffect(() => {
    let alive = true
    void (async () => {
      try {
        const [st, tb] = await Promise.all([api.larkStatus(), api.larkTables()])
        if (!alive) return
        setCliAvailable(st.available)
        setCliPath(st.cli)
        setTables(tb.tables)
        const first = tb.tables.find(t => t.configured)
        setTarget(first ? first.id : tb.tables[0]?.id ?? '')
      } catch (e: any) {
        if (alive) setError(String(e?.message ?? '加载飞书配置失败'))
      } finally {
        if (alive) setLoading(false)
      }
    })()
    return () => { alive = false }
  }, [])

  const selected = useMemo(() => tables.find(t => t.id === target), [tables, target])
  // 异动预警的日期由后端缓存口径决定, 面板选的日期对它无效
  const dateDisabled = target === 'abnormal'
  const canPush = !!target && !!selected?.configured && cliAvailable !== false && !pushing

  const handlePush = useCallback(async () => {
    setPushing(true)
    setError('')
    setResult(null)
    try {
      const r = await api.larkPush({
        strategy_id: target,
        as_of: dateDisabled ? undefined : (date || undefined),
        force,
        dry_run: dryRun,
        with_momentum: withMomentum,
        market,
      })
      setResult(r)
    } catch (e: any) {
      setError(String(e?.message ?? '推送失败'))
    } finally {
      setPushing(false)
    }
  }, [target, date, dateDisabled, force, dryRun, withMomentum, market])

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
        {...backdrop}
      >
        <motion.div
          initial={{ opacity: 0, scale: 0.95, y: 10 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.95, y: 10 }}
          transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
          className="w-[520px] max-h-[78vh] bg-surface border border-border rounded-card shadow-xl flex flex-col"
        >
          {/* 标题 */}
          <div className="flex items-center justify-between px-4 py-2.5 border-b border-border shrink-0">
            <span className="text-sm font-medium text-foreground">推送飞书</span>
            <button onClick={onClose} disabled={pushing}
              className="p-1 rounded hover:bg-elevated transition-colors cursor-pointer disabled:opacity-40">
              <X className="h-4 w-4 text-muted" />
            </button>
          </div>

          <div className="px-4 py-3 space-y-3 overflow-y-auto">
            {/* lark-cli 不可用: 直接说清楚, 别让用户点半天发现没通道 */}
            {cliAvailable === false && (
              <div className="flex items-start gap-2 px-3 py-2 rounded-btn border border-amber-400/20 bg-amber-400/10 text-[11px] text-amber-400">
                <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
                <span>未找到 lark-cli ({cliPath || 'lark-cli'}), 推送通道不可用。先在终端确认 <code>npm i -g @larksuiteoapi/lark-cli</code> 已安装并授权。</span>
              </div>
            )}

            {loading ? (
              <div className="flex items-center justify-center py-10">
                <div className="w-5 h-5 border-2 border-accent/30 border-t-accent rounded-full animate-spin" />
              </div>
            ) : (
              <>
                {/* 目标 */}
                <div className="space-y-1.5">
                  <label className="text-[11px] text-muted">推送目标</label>
                  <select
                    value={target}
                    onChange={e => { setTarget(e.target.value); setResult(null) }}
                    className="w-full h-8 px-2 rounded-btn border border-border bg-elevated text-xs text-foreground
                      outline-none focus:border-accent/50 cursor-pointer"
                  >
                    {tables.map(t => (
                      <option key={t.id} value={t.id}>
                        {presetNames?.[t.id] ?? t.label}{t.configured ? '' : ' (未配置表)'}
                      </option>
                    ))}
                  </select>
                  {selected && !selected.configured && (
                    <div className="text-[11px] text-amber-400">
                      该策略在飞书侧还没有对应表, 需要在 backend/app/services/lark_screener.py 的 STRATEGY_TABLES 里补 base_token / table_id
                    </div>
                  )}
                  {target === 'abnormal' && (
                    <div className="text-[11px] text-muted">异动预警按后端缓存日推送, 不受下面日期影响</div>
                  )}
                </div>

                {/* 日期 */}
                <div className="space-y-1.5">
                  <label className="text-[11px] text-muted">信号日期</label>
                  <input
                    type="date"
                    value={date}
                    min={minDate || undefined}
                    max={maxDate || undefined}
                    disabled={dateDisabled}
                    onChange={e => { setDate(e.target.value); setResult(null) }}
                    className="w-full h-8 px-2 rounded-btn border border-border bg-elevated text-xs text-foreground
                      outline-none focus:border-accent/50 disabled:opacity-50"
                  />
                  {!date && !dateDisabled && (
                    <div className="text-[11px] text-muted">留空则取最新交易日</div>
                  )}
                </div>

                {/* 选项 */}
                <div className="flex flex-wrap gap-x-4 gap-y-2 pt-0.5">
                  <label className="inline-flex items-center gap-1.5 text-[11px] text-secondary cursor-pointer">
                    <input type="checkbox" checked={dryRun} onChange={e => setDryRun(e.target.checked)}
                      className="accent-accent" />
                    试运行(只算不写入)
                  </label>
                  <label className="inline-flex items-center gap-1.5 text-[11px] text-secondary cursor-pointer">
                    <input type="checkbox" checked={force} onChange={e => setForce(e.target.checked)}
                      className="accent-accent" />
                    跳过去重强制推送
                  </label>
                  {target === 'trend_dragon' && (
                    <label className="inline-flex items-center gap-1.5 text-[11px] text-secondary cursor-pointer">
                      <input type="checkbox" checked={withMomentum} onChange={e => setWithMomentum(e.target.checked)}
                        className="accent-accent" />
                      补资金动能
                    </label>
                  )}
                </div>

                {/* 结果 */}
                {error && (
                  <div className="px-3 py-2 rounded-btn border border-danger/20 bg-danger/10 text-[11px] text-danger">
                    {error}
                  </div>
                )}
                {result && (
                  <div className={`px-3 py-2 rounded-btn border text-[11px] space-y-1 ${
                    result.ok ? 'border-emerald-400/20 bg-emerald-400/10 text-emerald-400' : 'border-danger/20 bg-danger/10 text-danger'
                  }`}>
                    <div className="flex items-center gap-1.5 font-medium">
                      {result.ok ? <CheckCircle2 className="h-3.5 w-3.5" /> : <AlertTriangle className="h-3.5 w-3.5" />}
                      {result.label} {result.as_of ? `· ${result.as_of}` : ''} {result.dry_run ? '(试运行)' : ''}
                    </div>
                    <div>选中 {result.selected} 只 · 推送 {result.pushed} 条 · 去重跳过 {result.skipped} 条</div>
                    {result.error && <div>{result.error}</div>}
                    {result.details.slice(0, 6).map((d, i) => (
                      <div key={i} className="text-muted break-all">{d}</div>
                    ))}
                    {result.sample && result.sample.length > 0 && (
                      <pre className="mt-1 p-2 rounded bg-elevated text-[10px] text-muted overflow-x-auto">
{JSON.stringify(result.sample, null, 2)}
                      </pre>
                    )}
                  </div>
                )}
              </>
            )}
          </div>

          {/* 底部操作 */}
          <div className="flex items-center justify-end gap-2 px-4 py-2.5 border-t border-border shrink-0">
            <button onClick={onClose} disabled={pushing}
              className="px-3 py-1.5 rounded-btn border border-border bg-surface text-xs text-muted
                hover:text-secondary transition-colors cursor-pointer disabled:opacity-40">
              关闭
            </button>
            <button onClick={() => void handlePush()} disabled={!canPush}
              title={selected?.configured ? '' : '该策略未配置飞书表'}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn text-xs font-medium
                border border-accent/30 bg-accent/10 text-accent hover:bg-accent/15
                transition-colors cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed">
              {pushing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}
              {pushing ? '推送中…' : dryRun ? '试运行' : '开始推送'}
            </button>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  )
}
