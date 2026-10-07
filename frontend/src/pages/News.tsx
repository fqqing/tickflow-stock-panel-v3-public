import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type FlashItem } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

type TopTab = 'flash' | 'stock'
type StockSub = 'news' | 'ann'

/** 个股新闻/公告列表行的统一宽松视图(两源字段交集, 全部可选) */
interface FeedRow {
  id?: string
  art_code?: string
  title: string
  date: string
  summary?: string
  source?: string
  url?: string
}

/** 东财快讯栏目 fastColumn -> 名称。102=全部(全球 7x24)为默认主频道。 */
const FLASH_COLUMNS = [
  { id: 102, label: '全部' },
  { id: 101, label: '要闻' },
  { id: 104, label: '公司' },
  { id: 105, label: '市场' },
  { id: 106, label: '机构' },
  { id: 107, label: '宏观' },
  { id: 108, label: '债券' },
  { id: 109, label: '基金' },
  { id: 110, label: '大宗' },
] as const

const PAGE = 50
const POLL_MS = 30_000

/** 时间戳: 今天显示 HH:mm, 否则 MM-DD HH:mm */
function fmtTime(t: string): string {
  const sp = t.split(' ')
  if (sp.length < 2) return t
  const today = new Date().toISOString().slice(0, 10)
  return sp[0] === today ? sp[1].slice(0, 5) : `${sp[0].slice(5)} ${sp[1].slice(0, 5)}`
}

/** 拆 summary 的【标题】前缀: 返回 head(标题) + body(正文) */
function splitSummary(s: string): { head: string; body: string } {
  const m = s.match(/^【(.+?)】(.*)$/)
  if (m) return { head: m[1], body: m[2].trim() }
  return { head: '', body: s }
}

/**
 * 资讯中心。
 * - 「电报」: 全市场快讯流(东财 getFastNewsList), 财联社/格隆汇电报同类。
 * - 「个股资讯」: 原有个股新闻/公告(自选股维度), 保留不阉割。
 */
export function News() {
  const [tab, setTab] = useState<TopTab>('flash')

  return (
    <div className="flex h-full min-h-0 flex-col p-3">
      <div className="mb-2 flex items-center gap-1">
        <button
          onClick={() => setTab('flash')}
          className={`rounded px-3 py-1 text-[12px] font-medium cursor-pointer transition-colors ${
            tab === 'flash' ? 'bg-accent text-white' : 'text-muted hover:text-secondary'
          }`}
        >
          电报
        </button>
        <button
          onClick={() => setTab('stock')}
          className={`rounded px-3 py-1 text-[12px] font-medium cursor-pointer transition-colors ${
            tab === 'stock' ? 'bg-accent text-white' : 'text-muted hover:text-secondary'
          }`}
        >
          个股资讯
        </button>
      </div>
      <div className="min-h-0 flex-1">
        {tab === 'flash' ? <FlashFeed /> : <StockNews />}
      </div>
    </div>
  )
}

/* ============================== 电报流 ============================== */

function FlashFeed() {
  const [column, setColumn] = useState(102)
  const [onlyImportant, setOnlyImportant] = useState(false)
  const [keyword, setKeyword] = useState('')
  const [items, setItems] = useState<FlashItem[]>([])
  const [sortEnd, setSortEnd] = useState('')
  const [hasMore, setHasMore] = useState(true)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [auto, setAuto] = useState(true)
  const [newCount, setNewCount] = useState(0)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  const scrollRef = useRef<HTMLDivElement>(null)
  const sentinelRef = useRef<HTMLDivElement>(null)
  const itemsRef = useRef<FlashItem[]>([])
  itemsRef.current = items
  const columnRef = useRef(column)
  columnRef.current = column

  // 加载第一页(重置列表)。append 时不清空(用于刷新顶部)。
  const loadFirst = useCallback(async (opts?: { append?: boolean }) => {
    setLoading(true)
    setError('')
    try {
      const r = await api.newsFlash(PAGE, '', columnRef.current)
      if (opts?.append) {
        // 去重后把最新条目插到顶部, 已加载的老条目保留在后面
        setItems(prev => {
          const known = new Set(prev.map(i => i.id))
          const fresh = r.items.filter(i => !known.has(i.id))
          return [...fresh, ...prev]
        })
      } else {
        setItems(r.items)
        setSortEnd(r.sortEnd)
        setHasMore(!!r.sortEnd && r.items.length > 0)
      }
      setNewCount(0)
    } catch {
      setError('电报加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  // 加载更多(翻页)
  const loadMore = useCallback(async () => {
    if (loading || !hasMore) return
    setLoading(true)
    try {
      const r = await api.newsFlash(PAGE, sortEnd, columnRef.current)
      setItems(prev => {
        const known = new Set(prev.map(i => i.id))
        const fresh = r.items.filter(i => !known.has(i.id))
        return [...prev, ...fresh]
      })
      setSortEnd(r.sortEnd)
      setHasMore(!!r.sortEnd && r.items.length > 0)
    } catch {
      /* 翻页失败静默, 保留已加载内容 */
    } finally {
      setLoading(false)
    }
  }, [loading, hasMore, sortEnd])

  // 切换频道 / 过滤条件时重置列表
  useEffect(() => { void loadFirst() }, [column, loadFirst])
  useEffect(() => { setExpanded(new Set()) }, [column, onlyImportant, keyword])

  // 无限滚动
  useEffect(() => {
    const el = sentinelRef.current
    const root = scrollRef.current
    if (!el) return
    const ob = new IntersectionObserver(
      entries => { if (entries[0].isIntersecting) void loadMore() },
      { root: root ?? null, rootMargin: '300px' },
    )
    ob.observe(el)
    return () => ob.disconnect()
  }, [loadMore])

  // 自动刷新: 轮询最新一页, 顶部时静默刷新, 否则提示新消息数
  useEffect(() => {
    if (!auto) { setNewCount(0); return }
    let alive = true
    const tick = async () => {
      try {
        const r = await api.newsFlash(20, '', columnRef.current)
        if (!alive) return
        const known = new Set(itemsRef.current.map(i => i.id))
        const fresh = r.items.filter(i => !known.has(i.id)).length
        const el = scrollRef.current
        const atTop = !el || el.scrollTop < 40
        if (fresh > 0) {
          if (atTop) void loadFirst({ append: true })
          else setNewCount(n => Math.max(n, fresh))
        }
      } catch { /* ignore */ }
    }
    const t = setInterval(tick, POLL_MS)
    return () => { alive = false; clearInterval(t) }
  }, [auto, loadFirst])

  const visible = useMemo(() => {
    const kw = keyword.trim().toLowerCase()
    let list = items
    if (onlyImportant) list = list.filter(i => i.important !== 0)
    if (kw) {
      list = list.filter(
        i => i.title.toLowerCase().includes(kw) || i.summary.toLowerCase().includes(kw),
      )
    }
    return list
  }, [items, onlyImportant, keyword])

  const toggleExpand = (id: string) =>
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const curColumn = FLASH_COLUMNS.find(c => c.id === column)?.label ?? '全部'

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden rounded border border-border bg-base">
      {/* 工具栏 */}
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-2 py-1.5">
        <div className="relative">
          <select
            value={column}
            onChange={e => setColumn(Number(e.target.value))}
            className="rounded border border-border bg-surface px-2 py-1 text-[11px] text-secondary outline-none cursor-pointer"
          >
            {FLASH_COLUMNS.map(c => (
              <option key={c.id} value={c.id}>{c.label}</option>
            ))}
          </select>
        </div>
        <button
          onClick={() => setOnlyImportant(v => !v)}
          className={`rounded px-2 py-1 text-[11px] cursor-pointer transition-colors ${
            onlyImportant
              ? 'bg-bull/15 text-bull'
              : 'text-muted hover:text-secondary'
          }`}
        >
          重磅
        </button>
        <input
          value={keyword}
          onChange={e => setKeyword(e.target.value)}
          placeholder="搜索电报…"
          className="w-40 rounded border border-border bg-surface px-2 py-1 text-[11px] text-secondary outline-none placeholder:text-muted focus:border-accent"
        />
        <button
          onClick={() => setAuto(v => !v)}
          className={`flex items-center gap-1 rounded px-2 py-1 text-[11px] cursor-pointer transition-colors ${
            auto ? 'text-accent' : 'text-muted hover:text-secondary'
          }`}
          title="自动刷新(30s 轮询)"
        >
          <span>⚡</span>
          <span>{auto ? '自动' : '手动'}</span>
        </button>
        <button
          onClick={() => void loadFirst()}
          className="ml-auto rounded px-2 py-1 text-[11px] text-muted hover:text-secondary cursor-pointer"
        >
          刷新
        </button>
        <span className="text-[10px] text-muted">
          {curColumn} · {items.length} 条
        </span>
      </div>

      {/* 新消息提示条 */}
      {newCount > 0 && (
        <button
          onClick={() => {
            void loadFirst()
            scrollRef.current?.scrollTo({ top: 0 })
          }}
          className="flex items-center justify-center gap-1 border-b border-accent/30 bg-accent/10 py-1 text-[11px] text-accent hover:bg-accent/15 cursor-pointer"
        >
          ↑ {newCount} 条新消息
        </button>
      )}

      {/* 时间流 */}
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto">
        {loading && items.length === 0 && (
          <div className="p-3 text-[11px] text-muted">加载中…</div>
        )}
        {error && items.length === 0 && (
          <div className="p-3 text-[11px] text-danger">{error}</div>
        )}
        {!loading && !error && visible.length === 0 && (
          <div className="p-3 text-[11px] text-muted">暂无电报</div>
        )}

        <ul className="divide-y divide-border/60">
          {visible.map(it => {
            const { head, body } = splitSummary(it.summary || it.title)
            const isOpen = expanded.has(it.id)
            const important = it.important !== 0
            return (
              <li
                key={it.id}
                onClick={() => toggleExpand(it.id)}
                className="group flex cursor-pointer gap-2 px-2 py-1.5 transition-colors hover:bg-elevated/60"
              >
                {/* 时间 */}
                <div className="w-14 shrink-0 pt-0.5 text-right font-mono text-[10px] leading-4 text-muted">
                  {fmtTime(it.showTime)}
                </div>
                {/* 圆点 + 内容 */}
                <div className="min-w-0 flex-1">
                  <div className="flex items-start gap-1.5">
                    <span
                      className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${
                        important ? 'bg-bull' : 'bg-muted/50'
                      }`}
                    />
                    <div className="min-w-0 flex-1">
                      {important && (
                        <span className="mr-1 rounded bg-bull/15 px-1 py-px align-middle text-[10px] font-medium text-bull">
                          重磅
                        </span>
                      )}
                      {head && (
                        <span
                          className={`text-[12px] font-medium leading-snug ${
                            important ? 'text-bull' : 'text-secondary'
                          }`}
                        >
                          {head}
                        </span>
                      )}
                      <span
                        className={`text-[12px] leading-snug ${
                          head ? 'text-secondary' : important ? 'text-bull' : 'text-secondary'
                        } ${!isOpen && body.length > 80 ? 'line-clamp-2' : ''}`}
                      >
                        {head ? (body ? ` ${body}` : '') : body}
                      </span>
                    </div>
                  </div>

                  {/* 关联标的 + 操作 */}
                  <div className="mt-1 flex flex-wrap items-center gap-1 pl-4">
                    {it.stocks.map(s => (
                      <Link
                        key={s.symbol}
                        to={`/stock/${encodeURIComponent(s.symbol)}`}
                        onClick={e => e.stopPropagation()}
                        className="rounded bg-accent/10 px-1.5 py-px font-mono text-[10px] text-accent hover:bg-accent/20"
                        title={`查看 ${s.symbol}`}
                      >
                        {s.code}
                      </Link>
                    ))}
                    {it.boards.map(b => (
                      <span
                        key={b}
                        className="rounded bg-elevated px-1.5 py-px font-mono text-[10px] text-muted"
                      >
                        {b}
                      </span>
                    ))}
                    {it.share > 0 && (
                      <span className="text-[10px] text-muted">分享 {it.share}</span>
                    )}
                    {it.url && (
                      <a
                        href={it.url}
                        target="_blank"
                        rel="noreferrer"
                        onClick={e => e.stopPropagation()}
                        className="text-[10px] text-muted opacity-0 transition-opacity hover:text-accent group-hover:opacity-100"
                      >
                        原文 ↗
                      </a>
                    )}
                    {body.length > 80 && (
                      <button
                        onClick={e => {
                          e.stopPropagation()
                          toggleExpand(it.id)
                        }}
                        className="text-[10px] text-accent"
                      >
                        {isOpen ? '收起' : '展开'}
                      </button>
                    )}
                  </div>
                </div>
              </li>
            )
          })}
        </ul>

        {/* 加载更多哨兵 */}
        <div ref={sentinelRef} className="h-1" />
        {loading && items.length > 0 && (
          <div className="p-2 text-center text-[11px] text-muted">加载更多…</div>
        )}
        {!hasMore && visible.length > 0 && (
          <div className="p-2 text-center text-[11px] text-muted">已加载全部</div>
        )}
      </div>
    </div>
  )
}

/* ============================== 个股资讯(原新闻/公告) ============================== */

function StockNews() {
  const [sub, setSub] = useState<StockSub>('news')
  const [symbol, setSymbol] = useState('')
  const [selId, setSelId] = useState('')

  const watchlist = useQuery({ queryKey: QK.watchlist, queryFn: api.watchlistList })
  const symbols = useMemo(() => watchlist.data?.symbols ?? [], [watchlist.data])

  const cur = symbol || symbols[0]?.symbol || ''
  const curName = useMemo(
    () => symbols.find(s => s.symbol === cur)?.name ?? '',
    [symbols, cur],
  )

  const news = useQuery({
    queryKey: ['news-stock', cur, curName],
    queryFn: () => api.newsStock(cur, curName || undefined),
    enabled: !!cur && sub === 'news',
  })
  const ann = useQuery({
    queryKey: ['news-ann', cur],
    queryFn: () => api.newsAnn(cur),
    enabled: !!cur && sub === 'ann',
  })

  useEffect(() => { setSelId('') }, [cur, sub])

  const items: FeedRow[] = sub === 'news' ? (news.data?.items ?? []) : (ann.data?.items ?? [])
  const selected = items.find(it => (it.id ?? it.art_code) === selId)

  const annContent = useQuery({
    queryKey: ['news-ann-content', selId],
    queryFn: () => api.newsAnnContent(selId),
    enabled: sub === 'ann' && !!selId,
  })

  const qc = useQueryClient()

  return (
    <div className="flex h-full min-h-0 gap-3">
      {/* 左: 自选股 */}
      <aside className="flex w-44 shrink-0 flex-col overflow-hidden rounded border border-border bg-base">
        <div className="border-b border-border px-2 py-1.5 text-[11px] font-medium text-secondary">
          自选 ({symbols.length})
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {symbols.map(s => (
            <button
              key={s.symbol}
              onClick={() => setSymbol(s.symbol)}
              className={`flex w-full items-baseline gap-1 px-2 py-1 text-left text-[11px] transition-colors ${
                s.symbol === cur
                  ? 'bg-accent/15 text-accent'
                  : 'text-secondary hover:bg-elevated'
              }`}
            >
              <span className="font-mono">{s.symbol.split('.')[0]}</span>
              <span className="truncate text-[10px] text-muted">{s.name ?? ''}</span>
            </button>
          ))}
          {symbols.length === 0 && (
            <div className="px-2 py-3 text-[11px] text-muted">自选为空</div>
          )}
        </div>
      </aside>

      {/* 中: 资讯流 */}
      <section className="flex min-w-0 flex-1 flex-col overflow-hidden rounded border border-border bg-base">
        <div className="flex items-center gap-1 border-b border-border px-2 py-1.5">
          <button
            onClick={() => setSub('news')}
            className={`rounded px-2 py-0.5 text-[11px] cursor-pointer transition-colors ${
              sub === 'news' ? 'bg-accent text-white' : 'text-muted hover:text-secondary'
            }`}
          >
            新闻
          </button>
          <button
            onClick={() => setSub('ann')}
            className={`rounded px-2 py-0.5 text-[11px] cursor-pointer transition-colors ${
              sub === 'ann' ? 'bg-accent text-white' : 'text-muted hover:text-secondary'
            }`}
          >
            公告
          </button>
          <span className="ml-2 text-[10px] text-muted">{curName || cur}</span>
          <button
            onClick={() => {
              qc.invalidateQueries({ queryKey: ['news-stock', cur] })
              qc.invalidateQueries({ queryKey: ['news-ann', cur] })
            }}
            className="ml-auto rounded px-2 py-0.5 text-[10px] text-muted hover:text-secondary cursor-pointer"
          >
            刷新
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {!cur && <div className="p-3 text-[11px] text-muted">请先加自选股</div>}
          {(news.isLoading || ann.isLoading) && (
            <div className="p-3 text-[11px] text-muted">加载中…</div>
          )}
          {(news.isError || ann.isError) && (
            <div className="p-3 text-[11px] text-danger">资讯加载失败</div>
          )}
          {items.map(it => {
            const id = it.id ?? it.art_code ?? ''
            return (
              <button
                key={id}
                onClick={() => setSelId(id)}
                className={`block w-full border-b border-border/60 px-2 py-1.5 text-left transition-colors ${
                  id === selId ? 'bg-elevated' : 'hover:bg-elevated/60'
                }`}
              >
                <div className="text-[11px] leading-snug text-secondary">{it.title}</div>
                <div className="mt-0.5 flex items-center gap-2 text-[10px] text-muted">
                  <span className="font-mono">{it.date}</span>
                  {it.summary && <span className="truncate">{it.summary.slice(0, 50)}…</span>}
                </div>
              </button>
            )
          })}
          {cur && !news.isLoading && !ann.isLoading && items.length === 0 && (
            <div className="p-3 text-[11px] text-muted">暂无资讯</div>
          )}
        </div>
      </section>

      {/* 右: 正文 */}
      <aside className="flex w-[380px] shrink-0 flex-col overflow-hidden rounded border border-border bg-base">
        <div className="border-b border-border px-2 py-1.5 text-[11px] font-medium text-secondary">
          正文
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
          {!selected && (
            <div className="text-[11px] text-muted">选中左侧一条查看内容</div>
          )}
          {selected && sub === 'news' && (
            <div>
              <div className="text-[12px] font-medium leading-snug text-secondary">
                {selected.title}
              </div>
              <div className="mt-1 text-[10px] text-muted">
                {selected.date} {selected.source || ''}
              </div>
              <p className="mt-2 whitespace-pre-wrap text-[11px] leading-relaxed text-secondary">
                {selected.summary || '（无摘要）'}
              </p>
              {selected.url && (
                <a
                  href={selected.url}
                  target="_blank"
                  rel="noreferrer"
                  className="mt-3 inline-block text-[11px] text-accent hover:underline"
                >
                  查看原文 →
                </a>
              )}
            </div>
          )}
          {selected && sub === 'ann' && (
            <div>
              <div className="text-[12px] font-medium leading-snug text-secondary">
                {selected.title}
              </div>
              <div className="mt-1 text-[10px] text-muted">{selected.date}</div>
              {annContent.isLoading && (
                <div className="mt-2 text-[11px] text-muted">正文加载中…</div>
              )}
              <pre className="mt-2 whitespace-pre-wrap font-sans text-[11px] leading-relaxed text-secondary">
                {annContent.data?.content || ''}
              </pre>
            </div>
          )}
        </div>
      </aside>
    </div>
  )
}
