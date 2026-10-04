/**
 * 缠论叠加层 hook：拿到某只标的的缠论结构，并按当前图表的日期序列裁剪成可绘制的图层。
 *
 * `chartDates` 必须是图表 x 轴的日期序列（升序、YYYY-MM-DD）。它同时决定了
 * 裁剪范围，所以切日期区间时叠加层会自动跟着变。
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { buildChanOverlay, EMPTY_CHAN_OVERLAY, type ChanOverlayLayers } from '@/lib/chan-overlay'

/**
 * 参与缠论计算的日K回溯根数。
 * 400 根足够涵盖 3~5 个中枢；再长对「当下买点」的判定没有增益，只是更慢。
 * 注意后端按 (symbol, lookback) 计算，与图表显示区间无关，所以这里取固定值。
 */
export const CHAN_OVERLAY_LOOKBACK = 400
/** 缠论笔/中枢会被后续行情改写，盘中缓存 5 分钟，避免每根新K都全量重算 */
const CHAN_OVERLAY_STALE_MS = 5 * 60_000

export function useChanOverlay(
  symbol: string | undefined,
  chartDates: string[],
  enabled: boolean,
): ChanOverlayLayers {
  const query = useQuery({
    queryKey: QK.chanAnalysis(symbol ?? '', CHAN_OVERLAY_LOOKBACK, true),
    queryFn: () => api.chanAnalysis(symbol as string, CHAN_OVERLAY_LOOKBACK, true),
    enabled: enabled && !!symbol,
    staleTime: CHAN_OVERLAY_STALE_MS,
    placeholderData: previous => previous,
  })

  return useMemo(
    () => (enabled ? buildChanOverlay(query.data, chartDates) : EMPTY_CHAN_OVERLAY),
    [enabled, query.data, chartDates],
  )
}
