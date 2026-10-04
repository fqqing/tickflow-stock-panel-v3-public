/**
 * 集中管理所有 React Query key。
 *
 * - 新增查询只需在此加一行，所有消费方自动引用。
 * - SSE invalidation 基于 SSE_INVALIDATE_PREFIXES 列表，新增 key 无需改 useQuoteStream。
 */

// ===== Query Key 工厂 =====

export const QK = {
  // 全局 / 共享 (Layout 预取)
  capabilities:   ['capabilities'] as const,
  settings:       ['settings'] as const,
  endpoints:      ['endpoints'] as const,
  version:        ['version'] as const,
  preferences:    ['preferences'] as const,
  dataSources:    ['data-sources'] as const,
  quoteStatus:    ['quote-status'] as const,
  quoteInterval:  ['quote-interval'] as const,
  overviewMarket: (asOf?: string, market?: string) => ['overview-market', asOf ?? 'latest', market ?? 'cn'] as const,
  indexQuotes:    ['index-quotes'] as const,
  indexList:      ['index-list'] as const,

  // Watchlist
  watchlist:            ['watchlist'] as const,
  watchlistGroups:      ['watchlist-groups'] as const,
  watchlistQuotes:      ['watchlist-quotes'] as const,
  watchlistEnriched:    (ext?: string) => ['watchlist-enriched', ext] as const,
  // 异动边缘总览 (开启监控时才查询, 参数为 min_closeness/limit)
  abnormalOverview:     (minCloseness: number, limit: number) => ['abnormal-overview', minCloseness, limit] as const,
  // 不用 watchlist- 前缀: 日K历史盘中几乎不变, 若被 SSE quotes_updated 高频失效
  // (expert 1s) 会导致全自选日K每秒重拉, staleTime 形同虚设。
  // 刷新点: staleTime 过期 + Watchlist 增删自选/改蜡烛天数时的手动失效;
  // 当日最后一根蜡烛由 Watchlist 用 enriched 实时 OHLC 前端修补 (零额外请求)。
  watchlistKlineBatch:  (symbols: string) => ['kline-batch', symbols] as const,
  // 不用 watchlist- 前缀: 避免被 SSE quotes_updated 高频失效(expert 1s/pro 2s)
  // 导致每次都拉 TickFlow 触限流。分时图用固定 refetchInterval 刷新即可。
  minuteBatch:          (symbols: string) => ['minute-batch', symbols] as const,
  instrumentSearch:     (q: string, assetTypes?: string) => ['instrument-search', q, assetTypes ?? 'stock'] as const,

  // Screener
  screener:             ['screener'] as const,
  screenerStrategies:   (assetType: string = 'stock') => ['screener-strategies', assetType] as const,
  // 摘要也按日期区分: 后端按日期槽回数据, 不带 asOf 会让 queryKey 与内容不匹配,
  // 切日期后仍展示上一个日期的命中数。
  screenerCachedSummary: (market: string = 'cn', asOf?: string) => ['screener-cached', 'summary', market, asOf ?? ''] as const,
  screenerCachedResult: (strategyId: string, asOf?: string, ext?: string, market: string = 'cn') => ['screener-cached', 'strategy', strategyId, asOf ?? '', ext ?? '', market] as const,
  screenerCached:       (asOf?: string, ext?: string, market: string = 'cn') => ['screener-cached', 'all', asOf ?? '', ext ?? '', market] as const,
  screenerKlineBatch:   (symbols: string) => ['screener-kline-batch', symbols] as const,
  // 缠论买卖点批量标注: 仅取决于 symbol 集合 (口径/参数为常量), 后端有结果缓存。
  // 自选页与策略页共用同一缓存 (同一 symbol 集合 → 同一份结果)。
  screenerChanAnnotate: (symbols: string) => ['screener-chan-annotate', symbols] as const,
  marketSnapshot:       ['market-snapshot'] as const,
  limitLadder:          (asOf?: string) => ['limit-ladder', asOf] as const,

  // Backtest
  backtestStatus:       ['backtest-status'] as const,
  factorColumns:        ['backtest-factor-columns'] as const,
  miningRuns:           ['backtest-mining-runs'] as const,
  miningAvailability:   (assetType: string, profile: string, start: string, end: string) =>
                          ['backtest-mining-availability', assetType, profile, start, end] as const,
  miningRun:            (id: string) => ['backtest-mining-run', id] as const,
  miningResult:         (id: string) => ['backtest-mining-result', id] as const,
  miningConfig:         ['backtest-mining-config'] as const,
  researchCandidates:  ['research-candidates'] as const,
  strategyLinkOptions: (assetType?: 'stock' | 'etf') => assetType
    ? ['strategy-link-options', assetType] as const
    : ['strategy-link-options'] as const,
  strategyDetail:       (id: string) => ['strategy-detail', id] as const,

  // Data / Pipeline
  dataStatus:           ['data-status'] as const,
  pipelineJobs:         ['pipeline-jobs'] as const,
  pipelineJob:          (id: string) => ['pipeline-job', id] as const,
  extData:              ['ext-data'] as const,
  extDataRows:          (id: string, date?: string, limit?: number, columns?: string) => ['ext-data-rows', id, date, limit, columns] as const,
  dimensionMembers:     (id: string, field: string, value: string, date?: string) => ['dimension-members', id, field, value, date] as const,
  analysisMenus:        ['analysis-menus'] as const,
  analysisMenu:         (id: string) => ['analysis-menu', id] as const,

  // Kline
  // 周期(period)纳入 key: 日/周/月 K 是不同数据, 不能共用缓存
  kline:                (symbol: string, start: string, end: string, extColumns?: string, period?: string, adjust?: string) =>
                           ['kline', symbol, start, end, extColumns ?? '', period ?? 'day', adjust ?? 'qfq'] as const,
  // limit 纳入 key: 同一周期不同截断长度是不同数据(虽然尾部子集, 但缓存分开更安全)
  klineMinuteK:         (symbol: string, period: string, days: number, limit = 0) =>
                           ['kline-minute-k', symbol, period, days, limit] as const,
  stockLevels:          (symbol: string, days?: number) => ['stock-levels', symbol, days ?? 120] as const,
  // 图表外部事件(监控触发 / 回测买卖点)。与 kline 分开: 这两路有自己的
  // staleTime, 不该因为切周期/复权(会让 kline key 变化)被一起重拉。
  chartAlerts:          (symbol: string, days: number) => ['chart-alerts', symbol, days] as const,
  chartTrades:          (symbol: string) => ['chart-trades', symbol] as const,
  // 筹码分布: 按 symbol + 回望天数 + 档数缓存
  stockChips:           (symbol: string, days?: number, bins?: number) =>
                           ['stock-chips', symbol, days ?? 250, bins ?? 60] as const,
  // 缠论单票结构（笔/中枢/一二三买卖点），按 symbol + 回溯根数 + 笔口径缓存
  chanAnalysis:         (symbol: string, lookback: number, strict: boolean) =>
                           ['chan-analysis', symbol, lookback, strict] as const,
  // 缠论全市场买点扫描，按买点类型 + 新鲜度 + 笔口径缓存（后端侧也有结果缓存）
  chanScan:             (kinds: string, recentBars: number, strict: boolean) =>
                           ['chan-scan', kinds, recentBars, strict] as const,
  // 信号实验室：复盘任务与台账查询共用一个前缀，复盘完成后整体失效即可
  signalLab:            ['signal-lab'] as const,
  // 盘中脉搏：资金流/竞价/力道/题材/梯队/逐笔共用前缀
  pulse:                ['pulse'] as const,
  klineMinute:          (symbol: string, date: string) =>
                             ['kline-minute', symbol, date] as const,
  klineMinuteRange:     (symbol: string, days: number) =>
                             ['kline-minute-range', symbol, days] as const,
  indexDaily:           (symbol: string, start: string, end: string) =>
                           ['index-daily', symbol, start, end] as const,
  indexMinute:          (symbol: string, date: string) =>
                           ['index-minute', symbol, date] as const,

  // Schema
  extDataSchemaAll:     ['ext-data-schema-all'] as const,
  tableSchema:          (table: string) => ['table-schema', table] as const,

  // Custom Signals
  customSignals:        ['custom-signals'] as const,
  customSignalsOptions: ['custom-signals-options'] as const,

  // Monitor (监控规则 + 触发记录)
  monitorRules:         ['monitor-rules'] as const,
  monitorRuleOptions:   ['monitor-rule-options'] as const,
  alerts:               (source?: string) => ['alerts', source ?? ''] as const,

  // AI 大盘复盘
  reviewReports:        ['review-reports'] as const,

  // 概念涨幅轮动矩阵
  rpsRotation:          (days: number) => ['rps-rotation', days] as const,

  // 市场环境(Regime) — 日级离线计算, 不进 SSE 刷新
  regimeHistory:        (limit?: number) => ['regime-history', limit ?? 0] as const,
  regimeLatest:         ['regime-latest'] as const,
  regimeStates:         (days: number) => ['regime-states', days] as const,
  regimeCoverage:       ['regime-coverage'] as const,
  regimePhases:         (start?: string, end?: string) => ['regime-phases', start ?? '', end ?? ''] as const,
  regimeMainline:       (kind: string, start?: string, end?: string) => ['regime-mainline', kind, start ?? '', end ?? ''] as const,
} as const

// ===== SSE 应该 invalidate 的 key 前缀列表 =====
// 新增需要 SSE 推送的查询，只需在此加一行
//
// 注意: 策略页 (screener-cached) 不在此列表 —— 行情刷新时策略结果不变
// (非监控策略读盘后静态缓存, 监控策略由独立的 strategy_results_updated 事件在
// 重算完成后刷新)。若加入 'screener', 会导致每个行情 tick 双重刷新策略页,
// 且在 monitor "重算" 窗口内读到空结果, 造成策略列表闪烁 (变 0 → 空失效 → 又出现)。

export const SSE_INVALIDATE_PREFIXES = [
  // 精确前缀: 只命中自选页的实时数据 (quotes/enriched)。不能用宽泛的 'watchlist' ——
  // 会误伤 ['watchlist'] (自选列表) 和 ['watchlist-groups'] (分组配置, 只随手动操作变化)。
  // 旧设置里的 'watchlist' 单开关由 useQuoteStream 兼容读取。
  'watchlist-quotes',
  'watchlist-enriched',
  'quote-status',
  'index-quotes',
  'overview-market',
  'limit-ladder',
] as const
