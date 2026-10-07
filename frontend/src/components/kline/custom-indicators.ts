/**
 * v3 自定义指标（资金动能 / MACD 定量结构 / 蛟龙出海）→ klinecharts registerIndicator。
 *
 * 三个指标都是「后端算好、前端只渲染」：calc 直接从 KLineData 上读后端下发的
 * cm_value / ms_* / td_* / ma10 列，前端不重算（指标归属后端是项目硬规矩，
 * 复权在聚合之前，聚合后必须重算指标）。
 *
 * 对应 ECharts 侧 EChartsCandlestick 的 SUB_CHARTS（momentum / macd_struct）与
 * tdragon 主图标记，同一套口径与配色 —— v3 切内核到 klinecharts 后这三个指标
 * 整片缺失，这里用指标(而非 overlay)补齐：指标能自动开副图窗格、自动给 y 轴
 * 范围、自动显示信息栏，比 overlay 手绘更贴合「指标面板可增删」的交互。
 *
 * figure 能力边界（klinecharts 10.0.3 指标渲染器）：支持 line / bar / rect /
 * circle / text，不支持 polygon。所以蛟龙出海的箭头用 circle 圆点代替三角。
 */
import * as kc from 'klinecharts'

// ===== 配色（与 ECharts 侧常量一致，红涨绿跌语义色不随主题变）=====
const RED = '#F04438'
const GREEN = '#12B76A'
/** 资金动能面积填充（红上绿下，同同花顺口径 origin 0） */
const MOMENTUM_FILL_POS = 'rgba(240,68,56,0.45)'
const MOMENTUM_FILL_NEG = 'rgba(18,183,106,0.45)'
/** 资金动能强弱阈值（0.5 强 / 1.5 极强） */
const TH_STRONG = '#FACC15'
const TH_EXTREME = '#8B5CF6'
const DIF_COLOR = '#FACC15'
const DEA_COLOR = '#8B5CF6'
const MACD_POS = 'rgba(240,68,56,0.6)'
const MACD_NEG = 'rgba(18,183,106,0.6)'
/** 蛟龙出海: 生命线(MA10)用色; 信号标记色见 dragon-markers.ts */
const DRAGON_LINE = '#F04438'

/** 结构标注文案与配色：1 结构形成 / 2 钝化 / 3 消失 */
const STRUCTURE_LABELS: Record<number, string> = { 1: '结构形成', 2: '钝化', 3: '消失' }
const STRUCTURE_COLORS: Record<number, string> = { 1: '#F04438', 2: '#F59E0B', 3: '#8E8E96' }

function num(v: unknown): number | null {
  if (v == null) return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

// ===== 资金动能（副图）=====
interface MomentumData {
  cm: number | null
  thStrong: number | null
  thExtreme: number | null
}

// ===== MACD 定量结构（副图）=====
interface MacdQuantData {
  dif: number | null
  dea: number | null
  hist: number | null
  btext: number
  by: number | null
  ttext: number
  ty: number | null
}

// ===== 蛟龙出海（主图）=====
interface DragonData {
  ma10: number | null
  sig: number | null
  low: number | null
}

let registered = false

export function registerCustomIndicators(): void {
  if (registered) return
  registered = true

  // ── 资金动能：折线红绿分段 + 面积填充(origin 0) + 0.5/1.5 阈值虚线 ──
  kc.registerIndicator<MomentumData>({
    name: 'MOMENTUM',
    shortName: '资金动能',
    series: 'normal',
    shouldOhlc: false,
    precision: 2,
    calcParams: [],
    figures: [
      {
        key: 'cm',
        type: 'rect',
        baseValue: 0,
        styles: ({ data }) => {
          const v = data.current?.cm ?? 0
          return {
            style: 'fill',
            color: v >= 0 ? MOMENTUM_FILL_POS : MOMENTUM_FILL_NEG,
            borderColor: 'transparent',
          }
        },
      },
      {
        key: 'cm',
        title: '资金动能: ',
        type: 'line',
        styles: ({ data }) => {
          const v = data.current?.cm
          return { color: v != null && v >= 0 ? RED : GREEN, size: 1 }
        },
      },
      { key: 'thStrong', type: 'line', styles: () => ({ color: TH_STRONG, style: 'dashed', size: 1 }) },
      { key: 'thExtreme', type: 'line', styles: () => ({ color: TH_EXTREME, style: 'dashed', size: 1 }) },
    ],
    calc: (dataList) => dataList.map((d) => {
      const cm = num(d.cm_value)
      return { cm, thStrong: 0.5, thExtreme: 1.5 }
    }),
  })

  // ── MACD 定量结构：DIF/DEA 线 + MACD 柱(正负变色) + 底/顶结构标注文字 ──
  kc.registerIndicator<MacdQuantData>({
    name: 'MACD_QUANT',
    shortName: 'MACD定量结构',
    series: 'normal',
    shouldOhlc: false,
    precision: 3,
    calcParams: [],
    figures: [
      {
        key: 'hist',
        title: 'MACD: ',
        type: 'bar',
        baseValue: 0,
        styles: ({ data }) => {
          const v = data.current?.hist ?? 0
          const color = v >= 0 ? MACD_POS : MACD_NEG
          return { color, borderColor: color }
        },
      },
      { key: 'dif', title: 'DIF: ', type: 'line', styles: () => ({ color: DIF_COLOR, size: 1 }) },
      { key: 'dea', title: 'DEA: ', type: 'line', styles: () => ({ color: DEA_COLOR, size: 1 }) },
      {
        key: 'by',
        type: 'text',
        attrs: ({ data, coordinate }) => ({
          x: coordinate.current.x,
          y: coordinate.current.by,
          text: STRUCTURE_LABELS[data.current?.btext ?? 0] ?? '',
          align: 'center',
          baseline: 'bottom',
        }),
        styles: ({ data }) => ({ color: STRUCTURE_COLORS[data.current?.btext ?? 0] ?? '#8E8E96' }),
      },
      {
        key: 'ty',
        type: 'text',
        attrs: ({ data, coordinate }) => ({
          x: coordinate.current.x,
          y: coordinate.current.ty,
          text: STRUCTURE_LABELS[data.current?.ttext ?? 0] ?? '',
          align: 'center',
          baseline: 'top',
        }),
        styles: ({ data }) => ({ color: STRUCTURE_COLORS[data.current?.ttext ?? 0] ?? '#8E8E96' }),
      },
    ],
    calc: (dataList) => dataList.map((d) => {
      const btext = Number(d.ms_btext ?? 0)
      const ttext = Number(d.ms_ttext ?? 0)
      return {
        dif: num(d.ms_diff),
        dea: num(d.ms_dea),
        hist: num(d.ms_hist),
        btext,
        by: btext ? num(d.ms_by) : null,
        ttext,
        ty: ttext ? num(d.ms_ty) : null,
      }
    }),
  })

  // ── 蛟龙出海：生命线 MA10(红色更醒目) + 信号圆点(low 下方) ──
  // 注意不画 td_a3 文字: 它是「距上次 9 连阳的 bar 数」, 几乎每根 bar 都有值,
  // 画出来会铺满整图 —— 与 v1 口径一致(图上只有信号标记, a3 只在信息栏显示)。
  kc.registerIndicator<DragonData>({
    name: 'DRAGON',
    shortName: '蛟龙出海',
    series: 'price',
    shouldOhlc: true,
    precision: 2,
    calcParams: [],
    figures: [
      { key: 'ma10', title: '生命线: ', type: 'line', styles: () => ({ color: DRAGON_LINE, size: 1.6 }) },
    ],
    // ★ 信号点不在指标里画: klinecharts 10.0.3 指标渲染器对「条件性显示」的
    //   figure(稀疏 key 或 dense key + attrs 条件)存在坐标管道问题(实测画不出/
    //   错位到可视区首根), dense 的 line 正常。信号点由 dragon-markers overlay
    //   负责(与 signal/limit-up 标记同一套成熟管线)。
    calc: (dataList) => dataList.map((d) => ({
      ma10: num(d.ma10),
      sig: d.td_signal ? 1 : null,
      low: num(d.low),
    })),
  })
}
