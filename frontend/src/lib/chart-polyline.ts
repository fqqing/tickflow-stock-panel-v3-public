/**
 * 主图折线的密集化：把「顶点序列」变成与 x 轴逐 bar 对齐的数组。
 *
 * 为什么需要：ECharts 的 category 轴在 `data: [[date, price], ...]` 形式下按类目名查找，
 * 行为依赖版本；改用「与类目等长的数组 + 中间线性插值」则完全确定：
 *   - 相邻顶点之间的每个 bar 填入该直线上的精确价格 ⇒ 画出来是笔直的斜线
 *   - 首个顶点之前 / 末个顶点之后保持 null（'-'）⇒ 不会从图外拉一条线进来
 *
 * 这是缠论「笔」的绘制基础：一笔 = 两个端点的直线段。
 */

export interface PolylineVertex {
  date: string
  price: number
}

/** 缺失值的占位。ECharts 把 '-' 视为 null，配合 connectNulls:false 即断线。 */
export const POLYLINE_GAP = '-'

export function densePolyline(
  vertices: PolylineVertex[],
  barCount: number,
  indexOf: Map<string, number>,
): (number | string)[] {
  const out: (number | string)[] = new Array(barCount).fill(POLYLINE_GAP)
  if (barCount <= 0 || vertices.length < 2) return out

  const resolved: { i: number; p: number }[] = []
  for (const v of vertices) {
    const i = indexOf.get(v.date)
    if (i == null || i < 0 || i >= barCount) continue
    if (!Number.isFinite(v.price)) continue
    resolved.push({ i, p: v.price })
  }
  if (resolved.length < 2) return out

  resolved.sort((a, b) => a.i - b.i)

  for (let k = 0; k < resolved.length - 1; k++) {
    const a = resolved[k]
    const b = resolved[k + 1]
    const span = b.i - a.i
    // 同一根 bar 上的两个顶点（理论上不该出现）：只落第一个，避免 0 除
    if (span <= 0) {
      out[a.i] = a.p
      continue
    }
    for (let i = a.i; i <= b.i; i++) {
      out[i] = a.p + ((b.p - a.p) * (i - a.i)) / span
    }
  }
  const tail = resolved[resolved.length - 1]
  out[tail.i] = tail.p
  return out
}
