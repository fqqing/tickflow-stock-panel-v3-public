# yanwen/GP 高价值功能迁移评估（#83）

> 目标仓库：`D:\project\GP\yanwen\GP`（约 10000 行 Python + 20+ Notebook，作者 yanwen）
> 宿主：`tickflow-stock-panel`。评估日期 2026-09-30。
> 结论先行：**可迁移的高价值功能有 6 个，但它们全部依赖「分钟级/逐笔级数据」——
> 也就是 #85（eltdx 剩余能力）先供数，#83 才能落地。两者不是并列选项，是依赖链。**

---

## 1. 全景：yanwen/GP 有什么

| 目录 | 主题 | 规模 |
| --- | --- | --- |
| `prod_online/script/` | 生产化流水线（选股/板块/竞价/推送） | 22 个脚本，含 `OrderFlowAnalyzer.py`(1186 行) |
| `stock_analysis/` | Flask+SocketIO 实时分析站：ICT 结构 / 足迹图 / 订单流 | 19 个文件，4 个 Blueprint |
| `当日策列/` | 盘中分钟级策略：开盘曲线 / 尾盘强度 / 竞价强度 / 成交密度 | 11 个 |
| `回测/` | `BacktestEngine` V1~1.0.2 + 试盘策略 + EOR 盯盘 | 7 个 |
| `股票数据获取/` | 同花顺/东财板块爬虫、支撑压力位、主力评分 | 9 个 |
| `测试通达信/` | 通达信本地实验：逐笔 → 足迹图 HTML、板块竞价、背离扫描 | 13 个 |
| `相似股票分析/` | DTW 多维特征找最像的股票 | 1 个 |
| `选股策略/` + `趋势分析/` | 板块共振、趋势擒龙、涨停分析 | 5 个 |
| `飞书测试/` + `prod_online/config/` | 飞书多维表格/消息/图片推送封装 | 10 个 |

自带 `SCRIPT_USAGE.md`（约 400 行），每个脚本都写了用途/依赖/运行前置 —— **评估不需要读源码，读它就够**。

---

## 2. 宿主面板已经有什么（避免重复造轮子）

| 能力 | 面板现状 |
| --- | --- |
| 日K / 分钟K / 分时 | ✅ 多数据源插件（tushare / eltdx / tencent / stocksdk） |
| 选股（底部结构 / 向上趋势 / 趋势擒龙 / 异动） | ✅ 矩阵原生策略 + 每日飞书推送 |
| 缠论（笔/中枢/买卖点 + 事件研究） | ✅ 完整 |
| 回测 + 因子库（含 GTJA Alpha191 双后端） | ✅ **比 yanwen 的 BacktestEngine 更完整** |
| 信号实验室（战绩汇总 / 形态归因 / 分支对比） | ✅ 刚做完（C1/C2） |
| 飞书推送（多维表格通道 + 每日盘后自动推送） | ✅ **已覆盖 yanwen 的 feishu_utils 场景** |
| 实时快照 / 五档 | ✅ tencent qt；本轮新增 eltdx 快照（更正确，见 §4） |
| **资金流（主力净额/大单分档）** | ❌ **完全空白** |
| **逐笔成交 / 订单流 / 足迹图** | ❌ 空白 |
| **集合竞价（序列 + 强度评分）** | ❌ 空白 |
| **题材/概念热度榜、涨停梯队** | ❌ 空白 |
| **分时买卖力道、大单推价** | ❌ 空白 |
| 板块共振评分 | ⚠️ 有板块排行，无共振评分 |
| ICT 结构（BOS/CHOCH/FVG/Order Block） | ❌ 空白（有缠论，不冲突） |

---

## 3. 迁移候选：价值 / 工作量 / 供数

| # | 功能 | yanwen 参考实现 | 供数（eltdx，本轮实测） | 价值 | 工作量 |
| --- | --- | --- | --- | --- | --- |
| M1 | **资金流（主力净额 / 大中小单分档）** | 无（面板侧空白） | `money_flow.daily` **0.04s**，含 `main_net` / `main_ratio` / 16 个分档桶 | ★★★ | 小 |
| M2 | **集合竞价强度**（序列 + 开盘量额 + 板块竞价） | `fetch_call_auction.py` / `tdx_auction.py` / `当日策列/竞价强度分析.py` | `auctions.series` **0.06s**（118 点）+ `helpers.auction_data` **0.15s** | ★★★ | 中 |
| M3 | **分时买卖力道 / 大单推价** | `全天股票分析.py` / `buy_sell_strength` 用法 | `helpers.buy_sell_strength` **0.03s**（240 点，逐分钟主买/主卖） | ★★ | 小 |
| M4 | **题材热度 + 个股题材** | 无（yanwen 走同花顺爬虫，脆弱） | `stock_topics` **0.37s**（20 个题材带理由）；`theme_strength_rank` **137s**（涨停数/最高连板/封单额/龙头） | ★★★ | 中（慢接口需落缓存 + 后台任务） |
| M5 | **涨停梯队 / 连板** | `import_limit_up_data.py` / `聚焦涨停` | `limit_ladder` **113s**（含 beta60d/pe_ttm/流通股/封单，实为短线指标全集） | ★★ | 中（同上，需缓存） |
| M6 | **逐笔 / 订单流 / 足迹图（Delta·CumDelta）** | `OrderFlowAnalyzer.py`(1186) + `orderflow_blueprint` + `测试通达信/order_link2.py` | `trades.today` **0.02s**（含 price/volume/order_count/**side**） | ★★★ | **大**（逐笔落盘 + 前端 canvas 渲染） |
| M7 | 板块共振评分 | `block_analysis.py`(495) / `选股策略/板块共振.py` | 本地日K即可算 | ★★ | 中 |
| M8 | 支撑压力位 + VWAP | `build_support_resistance_table.py` | 需 tick | ★★ | 中 |
| M9 | ICT 结构（Swing/BOS/CHOCH/FVG/OB） | `ict_blueprint` + `ict_stock_analyzer_plotly.py`(376) | 本地日K | ★ | 大 |
| M10 | 相似股票（DTW） | `寻找相似股票.py` | 本地 | ★ | 中 |
| — | 回测引擎 / 飞书推送 / 选股流水线 | — | — | **不迁移**（面板已有更好的） | — |

**建议顺序：M1 → M3 → M2 → M4/M5（同一套「慢接口缓存」基建）→ M6 → 其余。**
理由：M1/M3 是「零依赖 + 秒级接口 + 面板空白」，一天内可交付；M6 是 yanwen 最值钱的家底，
但它要先把逐笔落盘链路建起来（新的数据域，非插件 dataset 契约能覆盖），单独排期。

---

## 4. 本轮顺带修掉的现存缺陷（#85 第一批）

在评估过程中实测了 eltdx 全部剩余接口，发现它可以直接替掉现有实时源的一个已知坑：

- 腾讯 qt 实时（面板现用）**对科创板 688/689 的 `vol` 单位是「股」不是「手」**
  （memory 已记录：切腾讯做实时源会让科创板重蹈覆辙）。
- 本轮给 eltdx 插件加了 `realtime` dataset，分板块量纲自检结果：
  `amount/(volume*100)/close` 在 000/001/002/003/300/301/302/600/601/603/605/**688/689/920**
  全部落在 **0.989 ~ 1.014** —— **没有板块差异**。
- 性能：全市场 **7251 只 / 4.5s**（80 只一批 × 8 并发；80 是硬上限，实测 100/200/400 只请求都只回 80 只，
  800 只会把 7709 连接打挂）。
- 附带能力：快照自带**五档盘口**、内外盘（`inside_dish`/`outer_disc`）、现手、竞价成交额。

---

## 5. 复用探针

```bash
cd backend
.venv/Scripts/python.exe scripts/probe_eltdx_capabilities.py                 # 全量
.venv/Scripts/python.exe scripts/probe_eltdx_capabilities.py --only trades    # 只看逐笔+竞价
.venv/Scripts/python.exe scripts/verify_source_units.py --source eltdx --dataset realtime
```

探针已固化在仓库（不是临时脚本），后续接 M1~M6 时直接复跑确认上游没变。
