/**
 * K 线默认日期范围(近 6 个月)。
 *
 * 从 StockDailyKChart 抽出的纯函数: 终端(StockTerminal)需要它来初始化 dateRange,
 * 但 StockDailyKChart 本身 import 了 EChartsCandlestick —— 终端为了拿一个纯函数
 * 而连带加载 echarts(1.27MB chunk)不划算, 所以抽到无依赖的 lib 层。
 */
export function getDefaultRange(): { start: string; end: string } {
  const now = new Date()
  const end = now.toISOString().slice(0, 10)
  const s = new Date(now)
  s.setMonth(s.getMonth() - 6)
  const start = s.toISOString().slice(0, 10)
  return { start, end }
}
