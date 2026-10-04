# 缠论叠加层迁移方案（ECharts → 新图表内核）

> 配套文档：`docs/frontend-redesign-plan.html`（整体三阶段路线）
> 本文只讲**缠论**在 P1 换内核时怎么迁、风险在哪、怎么验收、怎么回退。

## 结论先行

**缠论是这次迁移里风险最低的部分**，不是最高的。

因为缠论的**计算完全在后端**（`backend/app/indicators/chan.py`，纯函数 + numpy 逐 bar 顺序算法），前端只做三件事：拉结果、转成绘图原语、交给图表库画。换图表内核时：

- 后端 `chan.py` —— 一行不动
- 前端 `lib/chan-overlay.ts` 的转换逻辑 —— 基本不动
- 只有「原语 → 具体绘制指令」这一层要重写

真正难的是**主图 K 线 + 6 个副图 + 指标计算**（那才是 1718 行的主体），缠论只是挂在主图上的一层。

## 一、现状数据流

```
后端 /api/chan/analysis
    │  ChanAnalysis { strokes, centers, signals }
    ▼
lib/useChanOverlay.ts        (39 行)  查询 + 缓存 5 分钟 + 按图表日期序列裁剪
    ▼
lib/chan-overlay.ts          (180 行) 纯数据转换 —— 不含任何 ECharts 运行时
    │   笔     → ChartPolyline（向上笔红 / 向下笔绿）
    │   中枢   → ChartRange（色块）+ 2 条水平 ChartPolyline（ZG/ZD）
    │   买卖点 → ChartMarker（买点在 low 下方↑，卖点在 high 上方↓）
    ▼
lib/chart-polyline.ts        (55 行)  densePolyline: 顶点序列 → 与 x 轴等长的数组
    ▼
components/EChartsCandlestick.tsx  polylines / ranges / markers props
```

关键性质：**`buildChanOverlay()` 是纯函数**，输入 `ChanAnalysis` + 图表日期序列，输出四个原语数组。它不含 `echarts` 的 import，也不碰 DOM。

## 二、已完成的前置解耦（本次会话做掉）

四个原语类型（`ChartMarker` / `ChartPolyline` / `ChartPriceLine` / `ChartRange`）原先**定义在 `EChartsCandlestick.tsx` 里**，而 `chan-overlay.ts`、`StockDailyKChart.tsx`、`pages/` 多处都从那个巨石组件 import 它们。这意味着换内核时，光搬类型就会牵动全项目。

已抽出为 **`frontend/src/lib/chart-primitives.ts`**（与图表库无关的中间表示 IR）：

- `EChartsCandlestick.tsx` 改为从 lib 引入并原样 `export type` —— **所有现有 import 路径保持可用，零破坏**
- `lib/chan-overlay.ts` 改为从 lib 引入
- 组件行数 1718 → 1686

至此渲染层变成可替换的：

```
缠论 / 监控价位 / 手绘线  →  产出 chart-primitives 原语  →  由具体图表库适配
                                                          ├─ ECharts 适配器（现状）
                                                          └─ KLineChart 适配器（P1）
```

## 三、原语映射表

| 原语 | 现在的 ECharts 实现 | 换成 KLineChart 后 | 说明 |
|---|---|---|---|
| `ChartPolyline`（笔） | `densePolyline` 稠密化成等长数组 + `'-'` 占位 + `connectNulls:false` | overlay/indicator 里 `drawLine`，直接给两个端点的坐标 | **不再需要稠密化**，`chart-polyline.ts` 可删 |
| `ChartRange`（中枢色块） | `markArea`（只接受 xAxis 范围） | `drawRect` | KLineChart 能直接给 x/y 两个方向的范围，比 markArea 直观 |
| `ChartPolyline`（ZG/ZD 水平线） | 水平折线（**不能**用 `ChartPriceLine`） | `drawLine` + 虚线样式 | 见下方「ECharts 三坑」第 2 条 |
| `ChartMarker`（买卖点） | `markPoint` | `drawText` / `drawIcon` | 位置换算逻辑可复用 |

## 四、ECharts 的三个坑（换库后自动消失，但要确认新库没有同类问题）

1. **`markArea` 只接受 xAxis 范围** —— y 方向边界属于 `markLine` 的语法 `[[{yAxis},{yAxis}]]`。所以中枢矩形只能用 `ChartRange` 走 markArea，不能混用。
2. **y 轴 min/max 会把所有 `priceLines` 的 value 并入**（`axisMin/axisMax`）—— 历史中枢的 ZG/ZD 离现价很远，用 `ChartPriceLine` 画会把纵轴拉爆。这是缠论中枢改用「水平折线」的原因。
   → 迁移时要**确认新内核画水平线不会撑开 y 轴范围**，否则同样的坑会再来一次。
3. **category 轴 + `data: [[date, price]]` 按名字查找，行为依赖版本** —— 所以必须稠密化成等长数组。新内核用时间戳坐标，无此问题。

## 五、实现路径（已核实 klinecharts@10.0.3 的真实 API）

> 本节结论来自实际下载 `klinecharts@10.0.3` 的 `dist/index.d.ts` 逐条核对，**不是猜测**。

**先说一个被推翻的判断**：我最初推测用「indicator 的 `draw` 钩子」自由绘制（因为能拿到 visibleRange 做裁剪）。核对后发现 **IndicatorTemplate 里根本没有 `draw` 方法**（`draw ?:` 零命中）——这条路走不通。

### 实际可用的路径：`registerOverlay` + `extendData`

> ⚠️ **v10 破坏性变更（P1 开工前必读）**：`chart.applyNewData(dataList)` 在 v10 里
> **已被移除**（`dist/index.esm.js` 里 `applyNewData` 零命中）。数据只能经
> `chart.setDataLoader({ getBars })` 进入，由 `setSymbol` / `setPeriod` / `resetData`
> 触发 `type: 'init' | 'forward' | 'backward' | 'update'` 的加载。
> 本项目是一次性拿全区间数据，适配只需 30 行：
> ```ts
> chart.setDataLoader({ getBars: ({ type, callback }) => {
>   if (type === 'init') callback(rowsRef.current, { backward: false, forward: false })
>   else callback([], false)
> } })
> ```
> 换股 = `setSymbol`；切周期 = `setPeriod`；重拉数据 = `resetData()`（三者都会重新走 init）。

类型定义里确认存在：

```
registerOverlay<E>(template: OverlayTemplate<E>): void
chart.createOverlay(value: string | OverlayCreate | Array<...>): Nullable<string> | Array<Nullable<string>>

OverlayCreateFiguresCallbackParams<E> = {
  chart: Chart
  overlay: Overlay<E>          ← 含 extendData: E, 缠论数据从这里进
  coordinates: Coordinate[]    ← 锚点已转好的像素坐标
  bounding: Bounding
  xAxis / yAxis
}

createPointFigures: (params) => OverlayFigure | OverlayFigure[]
```

图元 attrs 类型（对应 `type` 名）：

| type | attrs | 缠论用途 |
|---|---|---|
| `line` | `LineAttrs { coordinates: Coordinate[] }` | 笔（斜线）、中枢 ZG/ZD（水平线） |
| `polygon` | `PolygonAttrs { coordinates: Coordinate[] }` | 备选：不规则填充 |
| `rect` | `RectAttrs { x, y, width, height }` | 中枢色块 |
| `text` | `TextAttrs { x, y, text, align?, baseline? }` | 买卖点标签 |
| `circle` / `arc` | `CircleAttrs { x, y, r }` | 笔端点小圆点 |

`Overlay` 上还有两个关键字段：

- `totalStep: number` —— 「完成鼠标操作所需步骤数」。缠论是数据驱动、不需要用户点击，设为 `1`
- `lock: boolean` —— 锁定后不响应鼠标事件。**缠论 overlay 必须 `lock: true`**，否则会挡住图表的缩放拖拽

### 代码骨架

```ts
registerOverlay<ChanLayers>({
  name: 'chan',
  totalStep: 1,
  lock: true,
  createPointFigures: ({ overlay, coordinates, bounding }) => {
    const layers = overlay.extendData        // 缠论数据, 由 createOverlay 时传入
    const figs: OverlayFigure[] = []
    // 笔: 斜线, 端点坐标用 chart.convertToPixel 换算(笔端点可能落在可视区外)
    // 中枢: rect + 两条 line(ZG/ZD)
    // 买卖点: text + circle
    return figs
  },
})

// 挂载: 锚点给一个占位坐标, 绘制完全由 extendData 驱动
chart.createOverlay({
  name: 'chan',
  paneId: 'candle_pane',
  points: [{ timestamp: firstBarTs, value: firstBarClose }],
  extendData: chanLayers,
})

// 切股 / 切周期时: 先 removeOverlay 老的, 再 createOverlay 新的
```

### 最小验证的三件事 —— ✅ 已于 2026-09-24 全部验证通过（GO）

验证脚手架：`docs/kline-lab/kline-verify.html` + `run-verify.mjs`（headless Edge + CDP 直连）。
合成 400 根 K 线 + 24 笔 / 10 中枢 / 24 买卖点，实测结果：

| # | 待验证项 | 结果 | 实测数据 |
|---|---|---|---|
| 1 | 只给 1 个锚点时 `createPointFigures` 是否被调用 | ✅ 会 | `createPointFiguresCalls: 1`，返回 78 个图元 |
| 2 | `chart.convertToPixel` 在缩放/平移后是否仍正确 | ✅ 正确 | 往返 `convertFromPixel` 后 `timestamp` 全等、`value` 误差 **0.00**（缩放+左移后依然） |
| 3 | 水平线/色块会不会撑开 y 轴 | ✅ 不会 | 加叠加层前后 `yAxis.getRange()` 均为 `from 20.15 / to 21.71`，delta = 0 |

**为什么第 1 条必然成立**（读源码确认，不靠实测碰运气）：
`OverlayImp.override()` 里只要 `points.length > 0` 就把 `currentStep` 置为
`OVERLAY_DRAW_STEP_FINISHED(-1)` ⇒ `isDrawing()` 为 false ⇒ overlay 进 `_overlays`
（完整列表，而非只有一个槽位的 `_progressOverlayInfo`）；`OverlayView._drawOverlay`
的判定是 `coordinates.length > 0`。**所以「每笔一个 overlay」的兜底方案不需要了。**

### 附带验证（同样通过）

| # | 项 | 结果 |
|---|---|---|
| 4 | `removeOverlay` + 重新 `createOverlay`（切股/切周期） | ✅ `removeReturned: true`，remove 后 0 个、create 后 1 个 |
| 5 | 单帧绘制成本 | ✅ 78 图元 **0.5 ms/帧**（`redraws: 1`, `totalMs: 0.5`） |
| 6 | 端点落在可视区外 | `convertToPixel` 仍返回**有限数**但可能极大（实测 `x: -5440`、超远价 `y: -4.0e7`）⇒ 适配器里**必须自己按 `bounding` 裁剪**，否则 canvas 路径参数过大 |
| 7 | **x 轴等距性**（ECharts 逐 bar 插值 vs KLineChart 两端点直线的等价前提） | ✅ 相邻 bar 像素间距完全一致（`gapSpreadPx: 0`），索引中点像差 `0px` ⇒ 两个内核画的笔/中枢重合 |

> 注：库内**没有** `ctx.clip()`（全库零命中），越界靠各 pane 独立 canvas 的边界自然裁剪。

## 六、改动清单

| 文件 | 动作 | 状态 |
|---|---|---|
| `lib/chart-primitives.ts` | ✅ 已建（本次会话） | 已提交 |
| `lib/chan-overlay.ts` | ✅ 已去稠密化，输出原始顶点 | 已提交 |
| `lib/chart-polyline.ts` | 保留（ECharts 还在作为灰度/回退内核，等 ECharts 路径完全下线后再删） | — |
| `components/kline/KLinePro.tsx` | ✅ 新内核主图，挂载缠论 overlay + 价位线 overlay | 已提交 |
| `components/kline/chan-overlay-kline.ts` | ✅ KLineChart 适配器：原语 → overlay 图元（按 bounding 裁剪） | 已提交 |
| `frontend/scripts/chan-parity.ts` | ✅ 自动化对拍脚本（`pnpm parity`）：计数 / 逐笔 / 中枢 / 买卖点 / 稠密化一致性 / 开关 | 已提交 |

**不需要动**：`backend/app/indicators/chan.py`、`backend/app/api/chan.py`、`ChanScan.tsx`、自选页缠论单元格、`chan_structure` 策略 —— 全部与渲染层无关。

## 七、对拍验收

缠论的正确性不靠肉眼，靠对拍。项目里已有对拍脚本套路可复用（`tickflow-tdx-strategy-port` skill）。

1. **计数对拍**：同一 symbol、同一 lookback(400)，新旧两版渲染各自导出 `{ 笔数, 中枢数, 买点数, 卖点数 }`，要求**完全一致**
2. **逐笔对拍**：导出每笔的 `(start_date, start_price, end_date, end_price)`，逐条比对，允许 0 差异
3. **中枢对拍**：导出每个中枢的 `(start, end, zd, zg)`，逐条比对
4. **裁剪对拍**：切换日期区间（近1月 / 近6月 / 近3年），确认图外端点被正确裁剪、不会整条笔消失
5. **抽样人工核对**：10 只股票截图对比，重点看笔的端点是否落在分型上

## 八、回退机制

- **双轨并行**：`StockDailyKChart` 保留 ECharts 路径，新增 `KLinePro` 并行。
  ⚠️ 实际实现是**全局布尔开关**（`components/kline/useKLineProFlag.ts` 的 localStorage
  `tickflow.useKLinePro`，终端按 `g` 键切换），**没有**按 symbol 哈希分流 —— 也就是说
  灰度粒度是"整机开/关"，不能只让一部分标的走新内核。需要按比例灰度时得先补分流逻辑。
- **缠论独立开关**：缠论图层挂不挂，与用哪个内核解耦。即使新内核的 K 线本身有问题，缠论仍可在旧内核上看
- **备份点**：`backup/pre-terminal-20260924`（tag + branch + bundle + 物理文件），任何时候可整体回退

## 九、分步计划

| 步骤 | 内容 | 工作量 |
|---|---|---|
| 0 | ✅ 抽取 `chart-primitives.ts`（已完成） | — |
| 1 | ✅ **KLineChart overlay API 最小验证**（7 项全通过，闸门已开） | 已完成 |
| 2 | `chan-overlay.ts` 去稠密化，输出原始顶点 | 0.5 天 |
| 3 | ✅ 写 `ChanLayer` 适配器（`components/kline/chan-overlay-kline.ts`） | 已完成 |
| 4 | 对拍（计数 + 逐笔 + 中枢 + 裁剪） | 1 天 |
| 5 | 接入 `KLinePro`（已完成），删 `chart-polyline.ts` | 剩余 0.5 天 |

**原计划约 4.5 天，已完成 1+3+5 的大部分；剩余约 1.5 天（去稠密化 + 对拍 + 清理）。**
第 1 步闸门已过，兜底方案（每笔一个 overlay）确认不需要。

### 已落地的 P1 代码

| 文件 | 作用 |
|---|---|
| `components/kline/KLinePro.tsx` | KLineChart 主图：日/周/月 + VOL + MA + 缠论 + 价位线 |
| `components/kline/chan-overlay-kline.ts` | 原语 → overlay 适配器（笔=line / 中枢=rect+dashed / 买卖点=text） |
| `components/kline/price-line-overlay.ts` | 监控价位线 overlay |
| `components/kline/useKLineProFlag.ts` | 灰度开关（localStorage，不碰后端 Preferences schema） |

## 十、风险与不做的事

- **最大风险**：`registerOverlay` 本为「用户拖拽创建」设计，而缠论是纯数据驱动的非典型用法 —— 若锚点式 `points` 不触发 `createPointFigures`，需退到「每笔一个 overlay」的兜底（笔 ~30 + 中枢 ~8，overlay 数量百级，性能可接受，工作量 +1 天）。由第 1 步验证兜底，不影响其它部分。
- **不做**：把缠论计算搬到前端（后端已有成熟实现 + 事件研究结论，前端重算是重复造轮子且口径必然分叉）
- **不做**：迁移期间同时改缠论口径（严格笔/宽松笔参数一律不动，保证对拍可比）
