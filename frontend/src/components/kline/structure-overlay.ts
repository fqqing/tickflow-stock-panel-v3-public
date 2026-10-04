/**
 * 主图定量结构（EMA25/89 双轨 + 交叉图标 + 九转数字）→ KLineChart overlay。
 *
 * 对应 ECharts 侧 EChartsCandlestick 的 showStructure 分支(轨道带 / 变色双轨 /
 * BBB-SSS 箭头 / 九转数字)。此前这套信号**只有 ECharts 侧有**, 切到 KLinePro
 * 就整片消失 —— 那不是观感差异, 是功能缺失。这里按同一套口径复刻。
 *
 * 数据从 `chart.getDataList()` 读: 后端算好的 st_* 列由 KLinePro 在
 * rowToKLine 里挂到 KLineData 上(见 KLinePro.tsx), 前端不做任何重算 ——
 * 指标归属后端是项目硬规矩(复权在聚合之前, 聚合后必须重算指标)。
 *
 * 性能: 只画可视区间内的 bar(按 xAxis 反查时间戳裁剪), 轨道线把连续同色的
 * 段合并成一条折线, 避免逐 bar 建 figure。
 */
import * as kc from 'klinecharts'

/** 与 ECharts 侧 STRUCTURE_* 常量保持一致(换主题不改这几个语义色) */
const SHORT_UP = '#F04438' // 收盘在轨之上 → 红
const SHORT_DOWN = '#12B76A' // 收盘在轨之下 → 绿
const LONG_UP = '#F97066'
const LONG_DOWN = '#34D399'
const BAND_UP = 'rgba(240,68,56,0.10)'
const BAND_FLAT = 'rgba(148,163,184,0.10)'
const BAND_DOWN = 'rgba(18,183,106,0.10)'
const NINE_DN = '#F04438' // 下跌九转: 低位买点
const NINE_UP = '#12B76A' // 上涨九转: 高位卖点

/** st_icon: 4 收盘上穿短上轨 / 5 收盘跌破短下轨 */
const ICON_BREAKOUT = 4
const ICON_BREAKDOWN = 5

export interface StructurePayload {
  /**
   * 数据版本号。overlay 不靠 extendData 取数(数从 getDataList 读), 但需要一个
   * 变化信号触发 createPointFigures 重跑 —— rows/周期变化时 KLinePro 会改写它。
   */
  rev: string
}

interface Bar {
  ts: number
  x: number
  close: number
  high: number
  low: number
  /** 轨道值 + 已换算好的像素 y(轨道带与轨道线共用, 避免重复 convertToPixel) */
  dsg: number | null
  dsgY: number | null
  dxg: number | null
  dxgY: number | null
  csg: number | null
  csgY: number | null
  cxg: number | null
  cxgY: number | null
  icon: number
  dn: number
  up: number
}

function num(v: unknown): number | null {
  if (v == null) return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

/** 连续同色段合并: 返回 [{ color, coords }], 一段一条 figure */
function splitByColor(
  bars: Bar[],
  pick: (b: Bar) => { v: number | null; y: number | null },
  upColor: string,
  downColor: string,
): { color: string; coords: kc.Coordinate[] }[] {
  const out: { color: string; coords: kc.Coordinate[] }[] = []
  let cur: { color: string; coords: kc.Coordinate[] } | null = null
  for (const b of bars) {
    const { v, y } = pick(b)
    if (v == null || y == null) {
      cur = null // 缺值处断线(与 ECharts 的 connectNulls:false 同口径)
      continue
    }
    const color = b.close > v ? upColor : downColor
    if (!cur || cur.color !== color) {
      cur = { color, coords: [] }
      out.push(cur)
    }
    cur.coords.push({ x: b.x, y })
  }
  return out
}

let registered = false

export function registerStructureOverlay() {
  if (registered) return
  registered = true

  kc.registerOverlay<StructurePayload>({
    name: 'structure',
    totalStep: 1,
    lock: true,
    needDefaultPointFigure: false,
    needDefaultXAxisFigure: false,
    needDefaultYAxisFigure: false,
    createPointFigures: ({ chart, xAxis, bounding }) => {
      const data = chart.getDataList()
      if (data.length === 0) return []

      // ── 裁到可视区间 ──
      let from = 0
      let to = data.length - 1
      if (xAxis) {
        const left = xAxis.convertTimestampFromPixel(bounding.left)
        const right = xAxis.convertTimestampFromPixel(bounding.right)
        if (left != null && Number.isFinite(left)) {
          while (from < to && data[from].timestamp < left) from += 1
          from = Math.max(0, from - 1) // 多留一根, 避免左边缘缺口
        }
        if (right != null && Number.isFinite(right)) {
          while (to > from && data[to].timestamp > right) to -= 1
          to = Math.min(data.length - 1, to + 1)
        }
      }

      const P = (ts: number, v: number): kc.Coordinate | null => {
        const c = chart.convertToPixel({ timestamp: ts, value: v }, { paneId: 'candle_pane' })
        if (Array.isArray(c) || c == null || c.x == null || c.y == null) return null
        return { x: c.x, y: c.y }
      }

      const bars: Bar[] = []
      for (let i = from; i <= to; i += 1) {
        const d = data[i]
        const p = P(d.timestamp, Number(d.close))
        if (!p) continue
        const track = (raw: unknown): { v: number | null; y: number | null } => {
          const v = num(raw)
          if (v == null) return { v: null, y: null }
          const c = P(d.timestamp, v)
          return { v, y: c ? c.y : null }
        }
        const dsg = track(d.st_dsg)
        const dxg = track(d.st_dxg)
        const csg = track(d.st_csg)
        const cxg = track(d.st_cxg)
        bars.push({
          ts: d.timestamp,
          x: p.x,
          close: Number(d.close),
          high: Number(d.high),
          low: Number(d.low),
          dsg: dsg.v, dsgY: dsg.y,
          dxg: dxg.v, dxgY: dxg.y,
          csg: csg.v, csgY: csg.y,
          cxg: cxg.v, cxgY: cxg.y,
          icon: num(d.st_icon) ?? 0,
          dn: num(d.st_dn) ?? 0,
          up: num(d.st_up) ?? 0,
        })
      }
      if (bars.length === 0) return []

      const figs: kc.OverlayFigure[] = []

      // ── 轨道带: 逐 bar 矩形, 填充色随收盘价相对轨道的位置变化 ──
      const band = (
        upperKey: 'dsg' | 'csg',
        lowerKey: 'dxg' | 'cxg',
        upperYKey: 'dsgY' | 'csgY',
        lowerYKey: 'dxgY' | 'cxgY',
      ) => {
        for (let i = 0; i < bars.length; i += 1) {
          const b = bars[i]
          const u = b[upperKey]
          const l = b[lowerKey]
          const uy = b[upperYKey]
          const ly = b[lowerYKey]
          if (u == null || l == null || uy == null || ly == null) continue
          const w = i + 1 < bars.length ? Math.abs(bars[i + 1].x - b.x) : 6
          const y = Math.min(uy, ly)
          const h = Math.max(Math.abs(ly - uy), 0.5)
          const state = b.close > u ? 'up' : b.close < l ? 'down' : 'flat'
          figs.push({
            type: 'rect',
            attrs: { x: b.x - w / 2, y, width: Math.max(w, 1), height: h },
            styles: {
              style: 'fill',
              color: state === 'up' ? BAND_UP : state === 'down' ? BAND_DOWN : BAND_FLAT,
            },
          })
        }
      }
      band('csg', 'cxg', 'csgY', 'cxgY') // 长轨在下层
      band('dsg', 'dxg', 'dsgY', 'dxgY') // 短轨在上层

      // ── 变色双轨: 收盘在轨之上用红色, 之下用绿色(原公式两条互补线) ──
      const track = (
        key: 'dsg' | 'dxg' | 'csg' | 'cxg',
        yKey: 'dsgY' | 'dxgY' | 'csgY' | 'cxgY',
        upColor: string,
        downColor: string,
        size: number,
      ) => {
        for (const seg of splitByColor(bars, b => ({ v: b[key], y: b[yKey] }), upColor, downColor)) {
          if (seg.coords.length === 0) continue
          figs.push({
            type: 'line',
            attrs: { coordinates: seg.coords },
            styles: { color: seg.color, size, style: 'solid' },
          })
        }
      }
      track('dsg', 'dsgY', SHORT_UP, SHORT_DOWN, 1.6) // 短上轨
      track('dxg', 'dxgY', SHORT_UP, SHORT_DOWN, 1.6) // 短下轨
      track('csg', 'csgY', LONG_UP, LONG_DOWN, 1.2) // 长上轨
      track('cxg', 'cxgY', LONG_UP, LONG_DOWN, 1.2) // 长下轨

      // ── BBB/SSS 交叉图标: 三角形, 上穿画在 LOW 下方 / 下破画在 HIGH 上方 ──
      const triangle = (b: Bar, above: boolean, color: string) => {
        const price = above ? b.high : b.low
        const p = P(b.ts, price)
        if (!p) return
        const s = 4.5
        const y = above ? p.y - 6 : p.y + 6
        const tipY = above ? y - s : y + s
        figs.push({
          type: 'polygon',
          attrs: {
            coordinates: [
              { x: p.x, y: tipY },
              { x: p.x - s, y },
              { x: p.x + s, y },
            ],
          },
          styles: { style: 'fill', color },
        })
      }
      for (const b of bars) {
        if (b.icon === ICON_BREAKOUT) triangle(b, false, SHORT_UP)
        else if (b.icon === ICON_BREAKDOWN) triangle(b, true, SHORT_DOWN)
      }

      // ── 九转数字: 下跌九转 6~9 标 LOW 下方(红), 上涨九转 6~9 标 HIGH 上方(绿) ──
      for (const b of bars) {
        if (b.dn) {
          const p = P(b.ts, b.low)
          if (!p) continue
          figs.push({
            type: 'text',
            attrs: { x: p.x, y: p.y + 10, text: String(b.dn), align: 'center', baseline: 'top' },
            styles: { color: NINE_DN, size: 11, weight: 'bold' },
          })
        }
        if (b.up) {
          const p = P(b.ts, b.high)
          if (!p) continue
          figs.push({
            type: 'text',
            attrs: { x: p.x, y: p.y - 10, text: String(b.up), align: 'center', baseline: 'bottom' },
            styles: { color: NINE_UP, size: 11, weight: 'bold' },
          })
        }
      }

      return figs
    },
  })
}
