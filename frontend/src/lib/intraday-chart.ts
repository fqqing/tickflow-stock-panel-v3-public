import type { MinuteKlineRow } from '@/lib/api'

/**
 * 分钟K的 datetime → 分钟槽位键 (HH:MM, 北京时间)。
 *
 * 后端各数据源的分钟 datetime 口径并不统一 (tickflow 走 epoch → naive UTC;
 * stock-sdk / 自定义 HTTP 源给的是北京时间墙钟)。A 股连续竞价时段为北京
 * 09:30–15:00, 对应 UTC 01:30–07:00 —— 两者**没有交集**, 因此可以按小时
 * 无歧义地判别口径, 不必依赖时区字段:
 *   hour < 8  → 视为 UTC, +8 换到北京时间
 *   hour ≥ 8  → 已是北京时间, 原样使用
 */
export function formatMinuteTime(datetime: string): string {
  const match = datetime.match(/(\d{2}):(\d{2})/)
  if (!match) return datetime.slice(11, 16)
  let hour = parseInt(match[1])
  if (hour < 8) hour = (hour + 8) % 24
  return `${String(hour).padStart(2, '0')}:${match[2]}`
}

export function computeIntradayAverage(data: MinuteKlineRow[]): number[] {
  const result: number[] = []
  let amount = 0
  let volume = 0
  for (const row of data) {
    amount += row.amount
    volume += row.volume * 100
    result.push(volume > 0 ? amount / volume : row.close)
  }
  return result
}

function generateFullDayTimes(): string[] {
  const times: string[] = []
  for (let hour = 9; hour <= 11; hour++) {
    const startMinute = hour === 9 ? 30 : 0
    const endMinute = hour === 11 ? 30 : 59
    for (let minute = startMinute; minute <= endMinute; minute++) {
      times.push(`${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`)
    }
  }
  for (let hour = 13; hour <= 15; hour++) {
    const endMinute = hour === 15 ? 0 : 59
    for (let minute = 0; minute <= endMinute; minute++) {
      times.push(`${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`)
    }
  }
  return times
}

export const FULL_DAY_TIMES = generateFullDayTimes()
