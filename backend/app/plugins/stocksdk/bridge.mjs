#!/usr/bin/env node
/**
 * stock-sdk 桥接脚本。Python 后端通过 subprocess 调用它，用真实 stock-sdk 抓数据。
 *
 * Original implementation by @forrany (PR #57), migrated to plugin architecture.
 *
 * 协议:
 *   - stdin: 单行 JSON  { op, symbols?, adjust?, period?, start?, end?, concurrency?, boards? }
 *   - stdout: 单行 JSON
 *       daily/adj/minute: { ok:true, op, rows: { [appSymbol]: Row[] } }
 *       realtime/instruments/indexQuotes/industries: { ok:true, op, rows: Row[] }
 *       ping: { ok:true, op:'ping', version }
 *       失败:  { ok:false, error }
 *       部分失败: { ok:true, op, rows, errors: { [key]: msg } }  (key 为 symbol 或内部标记)
 *       附带元数据: { ok:true, op, rows, meta: {...} }           (如 industries 的板块完整率)
 *
 * op:
 *   daily        —— 日K(adjust 默认 none), 每个 symbol 一组 bars
 *   adj          —— 除权因子: 取 hfq 与 none 收盘价, ex_factor = close_hfq / close_none
 *   minute       —— 分钟K(period 默认 5)
 *   realtime     —— 全 A 股实时快照(batch.cn; **不含指数**)
 *   indexQuotes  —— 指定指数实时快照(quotes.cn, 需带 sh/sz 前缀)
 *   instruments  —— 全 A 股标的维表(batch.cn 提取元数据)
 *   industries   —— 东财行业板块成分股长表(board.industry.list + constituents)
 *   ping         —— 探活
 *
 * 说明: daily/adj/minute 的入参 symbols 是「app 符号」(如 600519.SH)。stock-sdk 能容错解析，
 * 返回结果里我们**回显原始 app 符号**作为 key，避免 code→符号 的歧义(指数/股票同码等)。
 * realtime/instruments 是全市场枚举，由 code + marketId 反推后缀。
 */
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { execSync } from 'node:child_process'
import path from 'node:path'

/**
 * 解析 stock-sdk 入口。ESM 的 bare import 只查本地 node_modules 链，不查全局，
 * 因此这里显式在 [本地(脚本旁), 全局 npm root, NODE_PATH] 中查找后动态 import。
 * 部署时优先用脚本旁 vendored 的 node_modules/stock-sdk。
 */
async function loadSDK() {
  const require = createRequire(import.meta.url)
  const scriptDir = path.dirname(fileURLToPath(import.meta.url))
  // 候选 node_modules 目录（按优先级）
  const nmDirs = [path.join(scriptDir, 'node_modules')]
  for (const p of (process.env.NODE_PATH || '').split(path.delimiter).filter(Boolean)) nmDirs.push(p)
  try {
    const groot = execSync('npm root -g', { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim()
    if (groot) nmDirs.push(groot)
  } catch {
    /* npm 不可用则忽略 */
  }
  let entry
  for (const nm of nmDirs) {
    try {
      entry = require.resolve(path.join(nm, 'stock-sdk'))
      break
    } catch {
      /* 试下一个 */
    }
  }
  if (!entry) {
    throw new Error(
      `无法解析 stock-sdk（已搜索: ${nmDirs.join(', ')}）。请在桥接目录 npm install，或全局 npm i -g stock-sdk。`
    )
  }
  const mod = await import(pathToFileURL(entry).href)
  return mod.StockSDK || (mod.default && mod.default.StockSDK)
}

const MARKET_ID_TO_SUFFIX = { '1': 'SH', '51': 'SZ', '62': 'BJ' }

// 下游(Python)可能提前关闭管道，忽略 EPIPE 避免噪声崩溃。
process.stdout.on('error', (e) => {
  if (e && e.code === 'EPIPE') process.exit(0)
})

function readStdin() {
  return new Promise((resolve, reject) => {
    let buf = ''
    process.stdin.setEncoding('utf8')
    process.stdin.on('data', (c) => (buf += c))
    process.stdin.on('end', () => resolve(buf))
    process.stdin.on('error', reject)
  })
}

/** 简单并发池: 对 items 逐个跑 worker，最多 concurrency 个在飞。 */
async function mapPool(items, concurrency, worker) {
  const results = new Array(items.length)
  let next = 0
  const runners = new Array(Math.min(concurrency, items.length)).fill(0).map(async () => {
    while (true) {
      const i = next++
      if (i >= items.length) return
      try {
        results[i] = await worker(items[i], i)
      } catch (e) {
        results[i] = { __error: String((e && e.message) || e) }
      }
    }
  })
  await runners.reduce((p) => p, Promise.resolve())
  await Promise.all(runners)
  return results
}

/** code + marketId → app 符号(600519.SH)。反推失败则退化用 code 前缀猜测。 */
function toAppSymbol(code, marketId) {
  const suffix = MARKET_ID_TO_SUFFIX[String(marketId)] || guessSuffix(code)
  return suffix ? `${code}.${suffix}` : String(code)
}

function guessSuffix(code) {
  const c = String(code)
  if (/^(6|5|9)/.test(c)) return 'SH'
  if (/^(0|3|1|2)/.test(c)) return 'SZ'
  if (/^(4|8|92)/.test(c)) return 'BJ'
  return ''
}

/**
 * app 符号(600519.SH / 000001.SH) → stock-sdk 代码(sh600519 / sh000001)。
 * quotes.cn 查指数必须带交易所前缀: 裸 '000001' 会被解析成平安银行(sz000001),
 * 拿到的是股票而不是上证指数。
 */
function fromAppSymbol(sym) {
  const m = String(sym || '').match(/^(\d{1,6})\.(SH|SZ|BJ)$/i)
  if (!m) return String(sym || '')
  return m[2].toLowerCase() + m[1]
}

/**
 * 给上游接口用的代码。与 fromAppSymbol 的区别: 裸代码(无交易所后缀)也会补前缀。
 *
 * ⚠️ `kline.cn`(日K) 会自己补交易所前缀, 所以裸代码 `600519` 或 app 格式
 * `600519.SH` 都能拿到数据; 但 `kline.cnMinute`(分钟) **不会** —— 传裸代码或
 * app 格式一律返回空数组。这正是「日K正常、分时图只有坐标轴没有线」的根因,
 * 故分钟路径必须显式给出 sh/sz/bj。
 */
function toUpstreamCode(sym) {
  const s = String(sym || '').trim()
  const m = s.match(/^(\d{1,6})\.(SH|SZ|BJ)$/i)
  if (m) return m[2].toLowerCase() + m[1]
  if (/^\d{1,6}$/.test(s)) {
    const suffix = guessSuffix(s)
    return suffix ? suffix.toLowerCase() + s : s
  }
  return s
}

/** stock-sdk 的 adjust 取值是 '' | 'qfq' | 'hfq'（无 'none'）。这里做兼容映射。 */
function normAdjust(v) {
  if (v === 'hfq') return 'hfq'
  if (v === 'qfq') return 'qfq'
  return '' // none / undefined / 空 → 不复权
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

/**
 * 桥接内累计的按 symbol 错误(每进程只跑一个 op, 无需按 op 区分)。
 * main() 会把它作为 payload.errors 一并输出, 让上游异常可见, 而不是被 mapPool
 * catch 掉之后"看起来只是这只票没数据"。
 */
const runtimeErrors = {}

/**
 * 桥接内累计的运行元数据(每进程只跑一个 op), 随 payload.meta 输出。
 * 目前供 industries 回报板块完整率 —— 调用方需要它来判断"这份快照够不够完整到
 * 可以覆盖旧表", 否则部分板块失败会产出一张看着完整、实则缺口的数据。
 */
const runtimeMeta = {}

/**
 * 包一层 fetchWithRetry: 失败时记账并返回空数组, 保证调用方总能拿到数组。
 * 必要性: mapPool 会把 worker 抛出的异常吞进 results[i](变成 {__error} 对象),
 * 若调用方在 await 之后才写 out[sym], 则该 key 永远不会被写入 —— 结果是
 * rows={} + Python 侧 source="none", 与"确实没数据"完全无法区分。
 */
async function fetchSafe(sym, fn, opts) {
  try {
    return await fetchWithRetry(fn, opts)
  } catch (e) {
    runtimeErrors[sym] = String((e && e.message) || e)
    return []
  }
}

/**
 * 上游(东财)偶发冷启动/限流时对活跃标的返回空数组(非报错)。对已知应有数据的请求，
 * 空结果重试若干次以提升鲁棒性；真正无数据(退市/停牌/区间无交易)时多花几次调用可接受。
 *
 * `backoff` 为每次重试的等待倍数(默认 1 = 固定间隔, 与原行为一致)。
 * 实测: 分钟接口存在十几秒级的间歇性空返回(单发成功率约 1/5), 固定 300ms 的密集重试
 * 会整段落在抽风窗口内; 退避后可跨过窗口。
 *
 * 抛错同样重试: 上游抖动有两种面孔 —— 返回空数组, 或直接抛
 * `SdkError: fetch failed`(连接层失败)。只重试前者的话, 一次连接抖动就会被当作
 * "确实没有数据", 且异常逃逸后 mapPool 会吞掉它(见 opMinute 注释), 表现为静默无数据。
 * 全部尝试都抛错时向上抛最后一个错误, 让 bridge.py 侧以告警形式可见。
 */
async function fetchWithRetry(fn, { retries = 2, delayMs = 300, backoff = 1 } = {}) {
  let last = []
  let lastErr = null
  let wait = delayMs
  for (let i = 0; i <= retries; i++) {
    try {
      const r = await fn()
      if (Array.isArray(r) && r.length > 0) return r
      last = Array.isArray(r) ? r : []
      lastErr = null
    } catch (e) {
      lastErr = e
    }
    if (i < retries) {
      await sleep(wait)
      wait = Math.max(1, Math.round(wait * backoff))
    }
  }
  if (lastErr) throw lastErr
  return last
}

async function fetchDaily(sdk, sym, { adjust, period = 'daily', start, end }) {
  const opts = { period, adjust: normAdjust(adjust) }
  if (start) opts.startDate = start
  if (end) opts.endDate = end
  // cn() 会自行补交易所前缀, 裸代码与 app 格式都能取到。失败记账后返回 []。
  return fetchSafe(sym, () => sdk.kline.cn(sym, opts))
}

async function opDaily(sdk, job) {
  const { symbols = [], adjust = 'none', period = 'daily', start, end, concurrency = 6 } = job
  const out = {}
  const rows = await mapPool(symbols, concurrency, (sym) =>
    fetchDaily(sdk, sym, { adjust, period, start, end })
  )
  symbols.forEach((sym, i) => {
    const r = rows[i]
    out[sym] = Array.isArray(r) ? r : []
  })
  return out
}

async function opAdj(sdk, job) {
  const { symbols = [], start, end, concurrency = 6 } = job
  const out = {}
  await mapPool(symbols, concurrency, async (sym) => {
    const [none, hfq] = await Promise.all([
      fetchDaily(sdk, sym, { adjust: 'none', start, end }),
      fetchDaily(sdk, sym, { adjust: 'hfq', start, end }),
    ])
    const noneByDate = new Map()
    for (const b of none) if (b && b.close) noneByDate.set(b.date, b.close)
    const factors = []
    for (const b of hfq) {
      if (!b || !b.date) continue
      const rawClose = noneByDate.get(b.date)
      if (!rawClose || !b.close) continue
      factors.push({ symbol: sym, trade_date: b.date, ex_factor: b.close / rawClose })
    }
    out[sym] = factors
    return factors
  })
  return out
}

async function opMinute(sdk, job) {
  const { symbols = [], period = 5, start, end, concurrency = 6 } = job
  const out = {}
  await mapPool(symbols, concurrency, async (sym) => {
    const opts = { period: String(period) }
    if (start) opts.startDate = start
    if (end) opts.endDate = end
    // 必须用 toUpstreamCode: cnMinute 不会像 kline.cn 那样自动补交易所前缀。
    // out 的 key 仍用原始 sym, 保证 Python 侧落盘的 symbol 列是 app 格式。
    // 退避重试: 该接口间歇性抽风(空数组 或 fetch failed), 固定短间隔跨不过窗口。
    // fetchSafe 保证即便全部重试失败也会写入 out[sym] = [], 不产生"缺 key"的静默态。
    const bars = await fetchSafe(
      sym,
      () => sdk.kline.cnMinute(toUpstreamCode(sym), opts),
      { retries: 4, delayMs: 400, backoff: 2 },
    )
    out[sym] = Array.isArray(bars) ? bars : []
    return out[sym]
  })
  return out
}

async function opRealtime(sdk, job) {
  const { concurrency = 8 } = job
  const all = await sdk.batch.cn({ concurrency })
  const rows = []
  for (const q of all || []) {
    if (!q || !q.code) continue
    rows.push({
      symbol: toAppSymbol(q.code, q.marketId),
      name: q.name,
      last_price: q.price,
      prev_close: q.prevClose,
      open: q.open,
      high: q.high,
      low: q.low,
      volume: q.volume,
      amount: q.amount,
      change_pct: q.changePercent,
    })
  }
  return rows
}

/**
 * 五档盘口(FullQuote)。quotes.cn 支持单只/批量股票代码，返回含 bid/ask 的完整快照。
 * 与 realtime(全市场 batch.cn) 不同，这里按调用方给的 symbols 逐个查，数据更全。
 */
async function opDepth(sdk, job) {
  const { symbols = [], chunkSize = 60 } = job
  if (!symbols.length) return []
  const codes = symbols.map(fromAppSymbol)
  const rows = []
  for (let i = 0; i < codes.length; i += chunkSize) {
    const chunk = codes.slice(i, i + chunkSize)
    const all = await fetchWithRetry(() => sdk.quotes.cn(chunk), { retries: 2, delayMs: 400, backoff: 2 })
    for (const q of all || []) {
      if (!q || !q.code) continue
      // 用「本次请求的 app 符号」回填 symbol，避免 code 歧义
      const want = chunk.find((c) => c.slice(2) === String(q.code))
      const symbol = want
        ? `${want.slice(2)}.${want.slice(0, 2).toUpperCase()}`
        : toAppSymbol(q.code, q.marketId)
      rows.push({
        symbol,
        name: q.name,
        last_price: q.price,
        prev_close: q.prevClose,
        open: q.open,
        high: q.high,
        low: q.low,
        volume: q.volume,
        amount: q.amount,
        change_pct: q.changePercent,
        turnover_rate: q.turnoverRate,
        pe: q.pe,
        pb: q.pb,
        total_market_cap: q.totalMarketCap,
        circulating_market_cap: q.circulatingMarketCap,
        limit_up: q.limitUp,
        limit_down: q.limitDown,
        volume_ratio: q.volumeRatio,
        avg_price: q.avgPrice,
        bid: q.bid || [],
        ask: q.ask || [],
        time: q.time,
        timestamp: q.timestamp,
      })
    }
  }
  return rows
}

/**
 * 指数实时快照。batch.cn(全 A 股)不含指数, 指数只能按码单查 quotes.cn。
 * 返回行形状与 opRealtime 一致, 便于 Python 侧复用同一套归一化。
 */
async function opIndexQuotes(sdk, job) {
  const { symbols = [], chunkSize = 60 } = job
  if (!symbols.length) return []
  const codes = symbols.map(fromAppSymbol)
  const rows = []
  for (let i = 0; i < codes.length; i += chunkSize) {
    const chunk = codes.slice(i, i + chunkSize)
    const all = await fetchWithRetry(() => sdk.quotes.cn(chunk), { retries: 1 })
    for (const q of all || []) {
      if (!q || !q.code) continue
      // 用「本次请求的 app 符号」回填 symbol: 指数代码跨市场可能重号(如 sh000001/sz000001),
      // 按 code 数字段在本次 chunk 内匹配, 保证 key 就是调用方传入的符号。
      const want = chunk.find((c) => c.slice(2) === String(q.code))
      const symbol = want
        ? `${want.slice(2)}.${want.slice(0, 2).toUpperCase()}`
        : toAppSymbol(q.code, q.marketId)
      rows.push({
        symbol,
        name: q.name,
        last_price: q.price,
        prev_close: q.prevClose,
        open: q.open,
        high: q.high,
        low: q.low,
        volume: q.volume,
        amount: q.amount,
        change_pct: q.changePercent,
      })
    }
  }
  return rows
}

async function opInstruments(sdk, job) {
  const { concurrency = 8 } = job
  const all = await sdk.batch.cn({ concurrency })
  const rows = []
  for (const q of all || []) {
    if (!q || !q.code) continue
    const suffix = MARKET_ID_TO_SUFFIX[String(q.marketId)] || guessSuffix(q.code)
    // 形状对齐 tickflow 的 Instrument(数值扩展字段放 ext),以复用 instrument_sync 的 flatten。
    rows.push({
      symbol: toAppSymbol(q.code, q.marketId),
      name: q.name,
      code: String(q.code),
      exchange: suffix,
      region: 'CN',
      type: 'stock',
      ext: {
        total_shares: q.totalShares ?? null,
        float_shares: q.circulatingShares ?? null,
        limit_up: q.limitUp ?? null,
        limit_down: q.limitDown ?? null,
      },
    })
  }
  return rows
}

/**
 * 行业分类摄取: 行业板块列表 + 逐板块成分股, 反推「个股 → 行业板块」长表。
 *
 * 用途: Alpha191 等行业中性化需要每个标的的行业归属。
 * ⚠️ 这是**东财行业板块(BK 编码)** 分类, 不是申万 —— 两者层级与命名均不同,
 *    若后续要做申万口径的中性化需另找数据源。
 * ⚠️ 东财行业板块本身分层(一级/二级/三级), 同一只票会命中多个板块。这里**如实
 *    存下全部 (symbol, board_code) 关系**、不做事后裁剪, 由消费方决定取哪一层 ——
 *    在摄取环节丢掉层级信息是不可逆的。
 *
 * 全量约 500 个板块 → 逐板块一次请求, 上游抖动下整轮可能耗时数分钟, 故重试给得较足。
 * 结果里通过 meta 回报板块完整率, 让调用方能拒绝"缺了板块的快照"。
 */
async function opIndustries(sdk, job) {
  const { concurrency = 6, boards = null } = job
  const all = await fetchSafe(
    '__industry_boards__',
    () => sdk.board.industry.list(),
    { retries: 5, delayMs: 800, backoff: 2 },
  )
  const wanted = boards && boards.length ? all.filter((b) => boards.includes(b.code)) : all
  const rows = []
  let ok = 0
  await mapPool(wanted, concurrency, async (b) => {
    const members = await fetchSafe(
      b.code,
      () => sdk.board.industry.constituents(b.code),
      { retries: 3, delayMs: 400, backoff: 2 },
    )
    // 空成分也算失败: 东财行业板块不该为空, 计成失败是保守方向(宁可拒绝覆盖)。
    if (members.length) ok += 1
    for (const m of members) {
      if (!m || !m.code) continue
      const code = String(m.code)
      rows.push({
        board_code: b.code,
        board_name: b.name,
        // 成分股 code 是 6 位数字且无 marketId, 按号段猜交易所前缀。
        symbol: toAppSymbol(code, undefined),
        code,
        name: m.name ?? null,
        board_change_pct: b.changePercent ?? null,
        price: m.price ?? null,
        change_pct: m.changePercent ?? null,
        turnover_rate: m.turnoverRate ?? null,
        pe: m.pe ?? null,
        pb: m.pb ?? null,
      })
    }
  })
  runtimeMeta.boards_total = all.length
  runtimeMeta.boards_requested = wanted.length
  runtimeMeta.boards_ok = ok
  return rows
}

async function main() {
  let job
  try {
    const raw = (await readStdin()).trim()
    job = raw ? JSON.parse(raw) : {}
  } catch (e) {
    process.stdout.write(JSON.stringify({ ok: false, error: `invalid job json: ${e.message}` }))
    return
  }
  const op = job.op || 'ping'
  try {
    const StockSDK = await loadSDK()
    if (!StockSDK) throw new Error('stock-sdk 已解析但未导出 StockSDK')
    if (op === 'ping') {
      process.stdout.write(JSON.stringify({ ok: true, op: 'ping', version: StockSDK.version || 'ok' }))
      return
    }
    const sdk = new StockSDK({ retry: { maxRetries: 3, baseDelay: 400 } })
    let rows
    switch (op) {
      case 'daily':
        rows = await opDaily(sdk, job)
        break
      case 'adj':
        rows = await opAdj(sdk, job)
        break
      case 'minute':
        rows = await opMinute(sdk, job)
        break
      case 'realtime':
        rows = await opRealtime(sdk, job)
        break
      case 'depth':
        rows = await opDepth(sdk, job)
        break
      case 'indexQuotes':
        rows = await opIndexQuotes(sdk, job)
        break
      case 'instruments':
        rows = await opInstruments(sdk, job)
        break
      case 'industries':
        rows = await opIndustries(sdk, job)
        break
      default:
        process.stdout.write(JSON.stringify({ ok: false, error: `unknown op: ${op}` }))
        return
    }
    const failed = Object.keys(runtimeErrors)
    const extra = {}
    // errors / meta 仅在非空时输出, 保持旧形状对既有调用方完全兼容。
    if (failed.length) extra.errors = runtimeErrors
    if (Object.keys(runtimeMeta).length) extra.meta = runtimeMeta
    process.stdout.write(JSON.stringify({ ok: true, op, rows, ...extra }))
  } catch (e) {
    process.stdout.write(JSON.stringify({ ok: false, op, error: String((e && e.stack) || e) }))
  }
}

main()
