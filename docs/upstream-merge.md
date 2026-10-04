# 合并上游（Upstream Merge）操作手册

本 fork（`hzy1522/tickflow-stock-panel`）在上游 `shy3130/tickflow-stock-panel` 基础上做
了**多市场扩展**（A股/港股/美股）。上游持续演进，本手册把首次大版本合并（v0.1.88 → v0.2.1，
67 提交）中验证过的流程与坑沉淀下来，供后续重复执行。

> 适用：把 `upstream/main` 合并进本 fork 的 `main`。
> 参考：`CONTRIBUTING.md`（工程规范）、`docs/secondary-development.md`（上游升级兼容约定）。

---

## 0. 前置：明确「为什么合」

- **cherry-pick（局部）**：只想拿上游的某些修复（如数据正确性、bugfix），且这些提交不依赖
  上游新架构。改动小、风险低、可逐个验证。
- **full merge（整体）**：想跟上上游主线（新功能、新架构）。冲突面大，且上游重构会要求
  重新评估 fork 的多市场扩展是否仍成立。
- 两者的收益/成本差异在第一次合并时摸过一次：67 提交全量 merge 产生了 16 个冲突文件，
  但**没有一个是「fork 功能被上游推翻」式的硬冲突**——多市场扩展的代码（`markets.py`、
  `price_limits.py`、`indices_market.py` 等）是 fork 新建文件，git 会原样保留。

## 1. 准备

```bash
# 1) 确认工作树干净
git status --porcelain          # 必须为空

# 2) 确保 upstream remote 存在且可 fetch（网络不通时, 本地缓存的上游引用可能足够)
git remote -v
git fetch upstream              # 失败也不一定阻塞: 见「网络受限」一节

# 3) 建合并分支, 不要直接在 main 上解冲突
git checkout -b sync-upstream main
```

**网络受限时**：本机曾出现 `git fetch upstream` 连不上 github 443，但本地缓存的上游引用
（`refs/remotes/upstream/main`）完整可用。先用 `git cat-file -e upstream/main` 验证引用完整性，
再决定是否离线合并。

## 2. 执行合并并盘点冲突

```bash
git merge --no-commit --no-ff upstream/main
git diff --name-only --diff-filter=U   # 冲突文件清单
```

**立刻确认 fork 新建文件安然无恙**（它们不该出现在冲突列表里）：

```bash
for f in backend/app/markets.py backend/app/price_limits.py backend/app/api/indices_market.py; do
  test -f "$f" && echo "OK $f" || echo "MISSING $f"
done
```

> 注意：`git diff HEAD..upstream/main` 里某个文件显示 `-N` 行，**不代表上游删了它**。
> 第一次合并时把 `markets.py` 的 `-205` 误读为「上游删除」，其实它只是 fork 新建、
> 上游从未有过的文件。判断依据是 `git cat-file -e <merge-base>:<path>` 是否存在。

## 3. 冲突解决顺序（从机械到语义）

按此顺序推进，把语义判断留到最后：

| 优先级 | 文件 | 处理方式 |
| :--- | :--- | :--- |
| 1 | `backend/uv.lock` | 依赖清单两侧一致时直接 `git checkout --theirs`；不一致则 `uv lock` 重解析 |
| 2 | `backend/pyproject.toml` | 版本对齐上游，保留 fork 描述与注释 |
| 3 | 纯 import / 文档注释 | 两侧并存，按需整理 |
| 4 | 行为冲突（引擎/API/UI） | 见 §4 语义合并模式 |
| 5 | `README.md` | 见 §6 |

每解一个文件立即 `git add`，解完跑一次该文件相关的测试，避免积压到最后一锅端。

## 4. 语义合并的五个模式（本次验证过的）

### 4.1 正交参数并存（回测引擎）

上游给缓存加 `generation`（enriched 快照失效检测），fork 已有 `market`（市场命名空间）。
两者不冲突——**同时进入缓存 key 与函数签名**：

```python
key = f"{market}:{asset_type}:{generation or 'unmanaged'}:{h}:{start}:{end}:{cols}"
```

注意 `generation` 标记按 `asset_type` 存、不区分市场：港美股拿到的是同一 token，只会造成
保守的额外失效，不会误判——写注释说明，后人不用再推演一遍。

### 4.2 门禁并存（API 校验）

上游 `research_only`（策略不可运行）与 fork `market` 兼容性（策略不适用于该市场）是两个
**正交的校验**，必须同时保留。绑定 `meta` 变量避免重复 `engine.get()`。

### 4.3 白名单字段并入（SSE 载荷）

fork 把 SSE 告警载荷抽成了纯函数 `rule_event_to_alert()`（显式白名单 + 可测），上游在同一
位置加了新字段 `abnormal_*`。**漏并 = 静默丢失上游功能**：

- 保留 `rule_event_to_alert()`（它让该契约可测）
- 把上游新增字段**并入** `_ALERT_PASSTHROUGH_KEYS`，而不是退回上游的内联字典
- 补一条契约测试，断言新字段能穿过白名单；用「临时删掉登记行 → 测试失败」证明非空洞

### 4.4 上游修复必须收（前端 query key）

上游修复了 SSE 前缀失效匹配问题（query key 必须拍平 spread，嵌套数组会失配）。fork 自己的
改动（`market` 维度）与它**兼容**，采用上游写法并保留 fork 维度——上游的 bugfix 不要因冲突
被我们自己的版本盖掉。

### 4.5 结构性 UI 重构 + fork 增量

上游重构了侧栏（状态卡收起时隐藏），fork 的 `MarketSwitcher` 是纯增量。处理原则：
**采用上游结构，把 fork 增量放进语义等价的位置**（如并入状态卡、跟随「收起时隐藏」）。
解完必须跑 `pnpm build`——第一次合并时在这里留了多余闭合标签，靠 TS 编译错误抓出来的。

## 5. 上游新测试对 fork 的适配

上游新增测试会用**假 repo**（如 `get_instruments_asset=lambda at: ...` 单参）。fork 把
`get_instruments_asset` 扩展成 `(asset_type, market="cn")` 后，引擎按双参调用——**改测试
替身而不是改引擎**：

```python
get_instruments_asset=lambda at, market="cn": instruments,
```

## 6. README 合并原则

- **以 fork 结构为主**：README 是 fork 自己的门面，「关于本项目」「新增功能」「已知限制」
  等身份段保留 fork 版本。
- **吸收上游新事实**：上游新功能（模块表、路线图、文档索引）合入后 fork 也具备，必须据实
  补齐，否则 README 与实际能力不符。
- **坏链优先避免**：上游重命名文件（如截图改中文名）时，fork 引用的旧路径已失效——**取
  上游那侧**，即使意味着放弃自己写的引用。
- **重复内容去重**：fork 已以「沿用自上游」方式引用的声明段，上游那侧再出现时就丢弃
  上游那侧，不要两处重复。
- **按既有决定处理敏感内容**：上游作者联系方式等，遵守本 fork 之前的取舍决定。

## 7. 验证矩阵（合入前必须全绿）

| 项 | 命令/标准 |
| :--- | :--- |
| 后端全量测试 | `uv sync --extra backtest --extra dev && uv run pytest`（v0.2.1 起 pytest 在 dev 组） |
| 多市场回归 | `uv run pytest tests/test_markets.py tests/test_strength_ladder_market.py tests/test_market_routing.py tests/test_limit_ladder_one_word.py tests/test_market_phase.py tests/test_market_mainline.py` |
| 关键符号导入 | `markets.py` 全套 + `_market_compatible_strategy` + `_limit_ladder_market` + `BacktestEngine` |
| 前端构建 | `cd frontend && pnpm build`（TS 类型检查） |
| ruff 增量 | 手解过的文件，合并版告警数 ≤ 合并前基线（`git show HEAD:<file>` 对比） |
| 启动冒烟 | `uv run uvicorn app.main:app --port 3099`，轮询就绪后打多市场 API（总览/美股环境/港股梯队/挖掘） |
| git 卫生 | `git diff --check`；全仓无 `<<<<<<<`/`>>>>>>>` 残留 |

**测试失败的三种归因**（务必区分，别都当自己改坏的）：

1. fork 签名变化导致的上游新测试替身不匹配 → 修替身（§5）
2. 上游既有 flaky（如 `test_mining_manager` 的取消竞态）→ 与 `upstream/main` 逐字 diff
   验证「非本次引入」，单独跑 3 次确认间歇性，记录在案
3. 真回归 → 回到 §4 检查合并逻辑

## 8. 合入与推送

```bash
git add -A && git commit    # merge 提交, 消息写明: 上游范围 + 冲突解决要点 + 验证结果
git checkout main
git merge --ff-only sync-upstream
git push origin main        # CI 只监听 main 分支推送
# 等 Tests + Docker 两个 workflow 绿后再宣告完成
```

> CI 触发条件见 `.github/workflows/test.yml`：仅 `branches: [main]`，`paths-ignore: *.md` 等。

## 9. 合入后的环境动作（容易忘）

1. **依赖组变了就同步文档**：上游移动依赖分组时，更新 `docs/deployment.md` 的
   `uv sync` 命令。
2. **重启本机 dev 环境**：后端 `uv run uvicorn app.main:app --reload --port 3018`、
   前端 `cd frontend && pnpm dev`，浏览器刷新。
3. **注意 data 目录的进程锁**：`mining_process_lock` 是单实例锁——同 data 目录只允许一个
   应用进程。换端口起第二个实例验证时会撞锁，属预期行为；验证完要确保旧实例已退出，
   否则正式实例起不来。
4. 提醒用户跑一遍真实链路（数据同步/选股/回测），比测试套件更能暴露环境差异。

## 10. 演进建议

- 本次合并解决了「fork 扩展 vs 上游 v0.2 架构」的首次碰撞。上游后续版本若改动
  `tickflow/repository.py`、指标流水线或策略引擎的接口，多市场扩展需要重新评估挂点。
- 长期看，把多市场扩展往上游的**扩展机制**（数据源插件化、前端插槽）上迁移，冲突面会
  持续缩小——但那是独立议题，不要在合并上游时顺手做。
