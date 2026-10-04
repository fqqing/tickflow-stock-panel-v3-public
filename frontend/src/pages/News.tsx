import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

type Tab = 'news' | 'ann'

/**
 * 资讯中心: 左侧自选股 + 中间资讯流 + 右侧正文。
 *
 * 数据源是东方财富公开接口(后端 /api/news, 带 5 分钟缓存)。
 * 新闻只有摘要(全文要跳原文链接), 公告正文按 art_code 单独取。
 */
export function News() {
  const [tab, setTab] = useState<Tab>('news')
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
    enabled: !!cur && tab === 'news',
  })
  const ann = useQuery({
    queryKey: ['news-ann', cur],
    queryFn: () => api.newsAnn(cur),
    enabled: !!cur && tab === 'ann',
  })

  // 切换标的或分类时清空右侧选中
  useEffect(() => { setSelId('') }, [cur, tab])

  const items = tab === 'news' ? (news.data?.items ?? []) : (ann.data?.items ?? [])
  const selected = items.find((it: { id?: string; art_code?: string }) =>
    (it.id ?? it.art_code) === selId)

  const annContent = useQuery({
    queryKey: ['news-ann-content', selId],
    queryFn: () => api.newsAnnContent(selId),
    enabled: tab === 'ann' && !!selId,
  })

  const qc = useQueryClient()

  return (
    <div className="flex h-full min-h-0 gap-3 p-3">
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
            onClick={() => setTab('news')}
            className={`rounded px-2 py-0.5 text-[11px] cursor-pointer transition-colors ${
              tab === 'news' ? 'bg-accent text-white' : 'text-muted hover:text-secondary'
            }`}
          >
            新闻
          </button>
          <button
            onClick={() => setTab('ann')}
            className={`rounded px-2 py-0.5 text-[11px] cursor-pointer transition-colors ${
              tab === 'ann' ? 'bg-accent text-white' : 'text-muted hover:text-secondary'
            }`}
          >
            公告
          </button>
          <span className="ml-2 text-[10px] text-muted">
            {curName || cur}
          </span>
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
          {items.map((it: { id?: string; art_code?: string; title: string; date: string; summary?: string }) => {
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
          {selected && tab === 'news' && (
            <div>
              <div className="text-[12px] font-medium leading-snug text-secondary">
                {(selected as { title: string }).title}
              </div>
              <div className="mt-1 text-[10px] text-muted">
                {selected.date} {(selected as { source?: string }).source || ''}
              </div>
              <p className="mt-2 whitespace-pre-wrap text-[11px] leading-relaxed text-secondary">
                {(selected as { summary?: string }).summary || '（无摘要）'}
              </p>
              {(selected as { url?: string }).url && (
                <a
                  href={(selected as { url?: string }).url}
                  target="_blank"
                  rel="noreferrer"
                  className="mt-3 inline-block text-[11px] text-accent hover:underline"
                >
                  查看原文 →
                </a>
              )}
            </div>
          )}
          {selected && tab === 'ann' && (
            <div>
              <div className="text-[12px] font-medium leading-snug text-secondary">
                {(selected as { title: string }).title}
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
