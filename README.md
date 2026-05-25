# SAP Smart Query Assistant

> 用自然语言查 SAP 业务数据 —— RAG 召回相关表，Agent 生成并自修复 SQL，再用业务语言解读结果。

## 项目背景

作者 2 年 ABAP 开发经验，最常被业务追着写 SE16N、做 ALV 报表。
这个项目把这类"日常被打扰"的工作交给 LLM，让业务用户能直接用中文问数据。

**为什么不是普通 Text-to-SQL**：SAP 的表名是 VBAK/VBAP/KNA1 这种德文缩写，
字段语义复杂、关联链路长，通用模型直接生成 SQL 准确率很低。本项目通过
**Schema RAG + 业务规则 Prompt + Agent 自修复** 把效果做上来。

## 技术栈

| 层 | 选型 |
|---|---|
| LLM | DeepSeek-V3 / V4（主力），Eval 阶段对比 GPT-4o-mini / 通义 / GLM-4 |
| 后端 | Python 3.10+ / FastAPI |
| 数据库 | SQLite（开发期）→ PostgreSQL（部署可切） |
| 向量库 | Chroma（本地嵌入式） |
| Embedding | BGE-small-zh（中文向量，本地推理） |
| Agent | LangGraph |
| 前端 | Streamlit |
| 部署 | Docker |

## 架构（最终形态）

```
用户问题 (中文)
     │
     ▼
┌──────────────────────────────────────┐
│  LangGraph Agent                     │
│                                       │
│  ① Schema RAG       (Chroma + BGE)   │
│       ↓                              │
│  ② SQL 生成         (DeepSeek)        │
│       ↓                              │
│  ③ SQL 执行         (SQLite)          │
│       ↓        失败                   │
│  ④ 自修复 ────┐                       │
│       ↓ 成功  │                       │
│  ⑤ 结果解读   │                       │
│       │      └──→ ② 重新生成          │
└───────┼──────────────────────────────┘
        ▼
   终端 / Streamlit
```

## 数据模型

**真实 SAP 表结构 + Faker 生成的 mock 业务数据**（合规、可演示、可开源）。
覆盖 SD/MM/FI 三大模块共 10 张核心表，约 3 万行业务数据。

| 模块 | 表 | 说明 |
|---|---|---|
| SD | VBAK / VBAP / KNA1 | 销售订单抬头/行项目 / 客户主数据 |
| MM | MARA / MAKT / LFA1 / EKKO / EKPO | 物料/物料描述/供应商/采购单 |
| FI | BKPF / BSEG | 财务凭证抬头/行项目 |

## Day 4 起的检索链路

```
用户中文问题
   │
   ▼
[Schema RAG]   BGE-small-zh 把问题向量化 → Chroma 余弦最近邻
               → 召回 top-5 表（每张约 200 token 的 schema 段落）
   │
   ▼
[Prompt 组装]  top-5 详细 schema + 10 张表"目录"（仅表名+一句话）
               + schema_lookup tool 指引
   │
   ▼
[SQL 生成 + Function Calling 循环]
               DeepSeek 看完 prompt：
                 ① 召回够 → 直接给 SQL
                 ② 召回缺 → 主动调 schema_lookup(["MAKT", ...]) 拉字段
                            → 拿到结果后再给 SQL
               最多 3 轮，防死循环
   │
   ▼ ...（Day 6 起接 LangGraph Agent，见下）
```

## Day 6 LangGraph SQL 自修复 Agent

```
                ┌─────────┐
START ───────►  │ generate│ ──► attempt 0：build_sql_messages
                └────┬────┘     attempt ≥ 1：build_repair_messages
                     │                    （把上次 SQL + error 塞进 user）
                     ▼
                ┌─────────┐     执行 run_sql：
                │ execute │ ──► 把 (sql, ok, error) 追加到 history
                └────┬────┘
                     │
                     ▼
            ok 或耗尽 max_repair_attempts ──► END
                     │
                     │ SQL 报错 且 attempt < max
                     ▼
                ┌─────────┐
                │ reflect │ ──► attempt += 1，回到 generate
                └─────────┘
```

设计要点：
1. generate 节点内部仍走 Day 4 的 `generate_sql_with_tools`，**保留 schema_lookup
   tool 调用能力** —— Day 4 经验：列名幻觉的标准补救动作就是先调 schema_lookup
   核字段，所以修复轮也应该有 tool 可用
2. reflect 节点目前只 `attempt += 1`。**独立成节点是为了 Day 7-8 扩展**（加 LLM
   反思一步、加 plan 节点、加 RAG 重召等），现在保持极简
3. 触发条件**只看 SQL 报错**。"SQL 跑通但 row_count=0"暂时不当失败（避免业务
   SQL 真没数据时被 Agent 误改成"加宽过滤"）—— 这类 Day 9 扩刁难题集时单独处理

## 快速开始

```bash
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.venv\Scripts\activate           # Windows PowerShell
pip install -r requirements.txt

# 2. 配置 API Key
copy .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY

# 3. 生成 mock 数据（一次性，约 30 秒）
python -m data.seed_data

# 4. 构建 Schema RAG 索引（Day 3 起必跑，首次会下载 BGE-small-zh ~95MB）
python -m scripts.build_index

# 5. 跑 demo（Day 6 起默认开 RAG + Function Calling + LangGraph Agent 自修复）
python -m src.main
# 自己提问：
python -m src.main "上个月销售额前 5 的客户是谁"
# 业务解读流式输出：
python -m src.main --stream "今年采购金额最高的 3 个供应商"
# 关掉 LangGraph 自修复，回到 Day 4 单轮 tool calling（对比用）：
python -m src.main --no-agent "..."
# 关掉 Function Calling，跑 Day 3 纯 RAG（对比用）：
python -m src.main --no-tools "..."
# 关掉 RAG 走 Day 2 老路（自动连带关 tool 和 agent）：
python -m src.main --no-rag "..."

# 6. 跑评测
python -m eval.retrieval_eval                       # RAG 召回率（top-1/3/5）
python -m eval.run_eval --use-agent --tag day7      # 端到端 Day 7-8 baseline (max_repair=3)
python -m eval.run_eval --use-agent --tag day6_repro --max-repair-attempts 2  # 复现 Day 6
python -m eval.run_eval --tag day4_repro            # 关 Agent 复现 Day 4
python -m eval.run_eval --no-tools --tag day3_repro # 关 tools 复现 Day 3
python -m eval.run_eval --no-rag --tag day2_repro   # 关 RAG 复现 Day 2
# 只看单题（调试用）：
python -m eval.run_eval --use-agent --case D9-11
# Agent 自修复链路 fault-injection 演示（即使本轮 baseline 没题触发也能看到效果）：
python -m scripts.agent_repair_demo
# Day 7-8 单元测试（_strip_code_fence + reflect 节点）：
python -m unittest tests.test_strip_code_fence tests.test_reflect_node -v
# 输出基线文件：eval/baseline_day7.json / baseline_day6.json / retrieval_day3.json
# 调用日志：    logs/runs.jsonl

# 7. Streamlit Web UI（Day 12）—— 浏览器演示同款链路
.venv\Scripts\python.exe -m streamlit run web/app.py
# 启动后访问 http://localhost:8501
# 侧边栏可切换 RAG / Tools / Agent 三个开关 + 调 top-K / 自修复次数
# 主区域会把 RAG 召回 / Tool calls / Agent attempts 全部可视化展示
```

## 14 天里程碑

- [x] **Day 1** MVP：项目骨架 + 10 张表 schema + mock 数据 + 端到端 query
- [x] **Day 2** 评测基线 + 流式输出 + 结构化调用日志
- [x] **Day 3** Schema RAG（Chroma + BGE-small-zh）：input token 1819 → 1117（-39%）
- [x] **Day 4** Function Calling（schema_lookup tool）：MAKT 漏召题（L4-01/L4-03）全部救回
- [x] **Day 6** LangGraph Agent + SQL 自修复（**核心亮点**）：通过率 14/15 → **15/15**，列名幻觉走自修复链路救回
- [x] **Day 12（提前）** Streamlit Web UI：把 RAG 召回 / Tool calls / Agent attempts 全部可视化，业务解读流式渲染
- [x] **Day 9（提前）** 扩 40 题刁难集 + RAG-only / Agent 双 baseline 对比
- [x] **Day 7-8** SQL 提取层 bug 修复 + 反思节点（重复错误检测）：40 题通过率 39 → **40/40 = 100%**
- [ ] **Day 10-11** 4 模型对比（DeepSeek / GPT-4o-mini / 通义 / GLM-4）
- [x] **Day 13-14** Docker 一键部署（单镜像烤入 BGE 模型 + SQLite + Chroma 索引，密码门保护）
- [ ] **Day 13-14** Demo 视频 + 技术博客

## Day 2 基线（DeepSeek-Chat / 全 schema 朴素 prompt / 2026-05-20）

| 指标 | 数值 |
|---|---|
| 通过率（粗判） | **15 / 15 = 100%** |
| 表分通过率 | 100% |
| 必含子句通过率 | 100% |
| 可执行率（SQL 跑通 + 非空） | 100% |
| 平均 input tokens | 1819 |
| 平均 output tokens | 78 |
| 平均端到端延迟 | 2046 ms |

**判分维度（T/M/A/E）**：表全中 / 必含子句全中 / 备选子句全中 / 可执行非空——避免死匹配
SQL 字符串导致评测变成"LLM 风格测试"。

## Day 3 RAG 召回率（BGE-small-zh-v1.5 / Chroma 余弦 / 10 张表 / 15 题）

| top-K | hit 率（全部 expected 都召回） | avg recall | 备注 |
|---|---|---|---|
| top-1 | 33.3% (5/15) | 57.8% | 单表题已经能命中过半 |
| top-3 | 80.0% (12/15) | 92.2% | L1/L2/L5 全中，L3-02 漏 LFA1 |
| **top-5** | **86.7% (13/15)** | **94.4%** | 仅 L4-01 / L4-03 漏 MAKT（物料中文描述表） |

**已知短板**：MAKT 表名/描述都太短（"物料描述（Material Descriptions）"），被同样含 MATNR
的 MARA/VBAP 盖住。这是 Day 5 Function Calling 要解决的 case —— 让 LLM 在召回缺表时
主动调 `schema_lookup("物料中文名称")` 兜底。

## Day 3 端到端基线（DeepSeek-Chat / Schema RAG top-5 / 2026-05-20）

| 指标 | Day 2（朴素） | **Day 3（RAG）** | 变化 |
|---|---|---|---|
| 通过率（粗判） | 15 / 15 = 100% | **14 / 15 = 93.3%** | -1 题（L5-03） |
| 平均 input tokens | 1819 | **1117** | **-38.6%** ↓ |
| 平均 output tokens | 78 | 78 | 持平 |
| 平均端到端延迟 | 2046 ms | 2006 ms | 略降 |
| 总 input tokens | 27287 | 16750 | -38.6% ↓ |

**Day 3 重要观察**：

1. **RAG 召回耗时可以忽略**：BGE-small-zh 首次加载 ~25s（一次性），后续查询稳定在
   20-50 ms / 题，相对 LLM 调用 1.5-2s 是噪声。
2. **token 压缩 39%，没到 ≤500 的理想值**：原因是 top-5 召回里每张表的字段定义还在
   ~200 token / 表。降到 ≤500 是 Day 4-5 的事 —— Function Calling 让 LLM 平均只
   摸 2-3 张表，叠加更紧凑的字段描述能再降一档。
3. **L5-03 由 PASS 变 FAIL（故事弹药）**：题目"对比 2025 销售总额和采购总额"，RAG
   召回 VBAK/EKKO/VBAP/EKPO/BKPF 五张表都对，但 LLM 生成的 SQL 出现了**列名幻觉**
   `EKKO.NETWR`（EKKO 没这列，正确写法是 JOIN EKPO 再用 EKPO.NETPR）。Day 2 全
   schema 因为对照完整没出错。**这正是 Day 6 SQL 自修复 Agent 的目标场景**：执行
   报错 `no such column: EKKO.NETWR` → Agent 看到错误信息 + 重新召回 → 修正 SQL。

**这两组基线锚定后续三段提升：**
- Day 4-5：input tokens 1117 → ≤500（Function Calling 按需召表 + 字段描述压缩）
- Day 6-8：自修复 Agent 把 L5-03 这类列名幻觉/JOIN 错的 case 救回来；在 Day 9 扩充的
  难题集上把成功率从 X% 拉到 ≥85%
- Day 9-11：扩充刁难题集 + 4 模型对比，画"准确率 × 成本"四象限

## Day 4 端到端基线（DeepSeek-Chat / RAG top-5 + schema_lookup tool / 2026-05-20）

| 指标 | Day 2（朴素） | Day 3（RAG） | **Day 4（RAG+Tools）** | 主要变化 |
|---|---|---|---|---|
| 通过率（粗判） | 15 / 15 = 100% | 14 / 15 = 93.3% | **14 / 15 = 93.3%** | L4-01/L4-03 救回，L5-03 仍顽固 |
| 表分通过率 | 100% | 86.7% | 93.3% | MAKT 漏召不再失分 |
| 必含子句通过率 | 100% | 100% | 100% | — |
| 备选子句通过率 | 100% | 100% | 100% | — |
| 可执行通过率 | 100% | 100% | 100% | — |
| 平均 input tokens | 1819 | 1117 | 2640 | 调 tool 的题 prompt 翻倍 |
| 平均 output tokens | 78 | 78 | 209 | tool message 计入 output |
| 平均端到端延迟 | 2046 ms | 2006 ms | 3873 ms | 多一轮调用 |
| Tool 使用率 | — | — | **40% (6/15)** | LLM 自主判断是否需要补字段 |

**Day 4 关键观察**：

1. **Function Calling 把 MAKT 漏召题救回来了**：Day 3 漏召的 L4-01（销售数量前 5
   物料带中文名）和 L4-03（供应商采购物料带中文名）全部回到 PASS，LLM 在拿到
   top-5 召回看不到 MAKT 时，**主动调用** `schema_lookup(["MAKT"])` 补齐字段，
   再 JOIN 生成正确 SQL。这是 RAG 召回上限的有效兜底。
2. **token 和延迟的代价是值得的**：平均 input token 1117 → 2640 主要来自两部分：
   ① 调 tool 的题 prompt 翻倍（schema_lookup 返回的字段定义被算进下一轮 prompt）；
   ② 不调 tool 的题也涨了 ~100 token（全表目录 + tool 指引段）。Day 5 会把字段描
   述压紧 + 关掉无效目录段，把平均 input 拉回 ≤1800。
3. **LLM 的工具判断基本健康**：40% 使用率，需要 MAKT 中文名的题几乎全调（5/6 次
   命中 MAKT，1 次命中 MARA），不需要外加表的题完全不调。
4. **L5-03 仍是顽固 case，留给 Day 6**：题目要"对比销售总额和采购总额"，这次
   LLM 偷懒只算了销售（output_tokens 1086 显示它"想了很多"但最后给出半截 SQL）。
   Function Calling 解决不了"漏意图"，Day 6 LangGraph Agent 才能在执行后看到
   "结果只有 1 行而题目说对比两边"重新生成。

## Day 6 端到端基线（DeepSeek-Chat / RAG top-5 + Tools + LangGraph Agent / 2026-05-21）

| 指标 | Day 3（RAG） | Day 4（+Tools） | **Day 6（+Agent）** | Day 6 主要变化 |
|---|---|---|---|---|
| 通过率（粗判） | 14/15 = 93.3% | 14/15 = 93.3% | **15/15 = 100%** | L5-03 救回 |
| 表分通过率 | 86.7% | 93.3% | **100%** | L5-03 这次完整 JOIN EKKO/VBAK |
| 平均 input tokens | 1117 | 2640 | 2792 | +5.7%（同档，主要来自 L5-03 加调 tool） |
| 平均 output tokens | 78 | 209 | 150 | -28% |
| 平均端到端延迟 | 2006 ms | 3873 ms | **2929 ms** | **-24%** ↓（output 少了，没人调多轮） |
| Tool 使用率 | — | 40% (6/15) | **46.7% (7/15)** | L5-03 这次主动调 schema_lookup |
| 自修复触发次数 | — | — | 0 / 15 | 本轮 LLM 首次全通过，**未触发** |

**Day 6 重要观察 —— 工程上的好结果，故事上的"问题"：**

1. **15 题首轮全通过，Agent 自修复一次都没触发**。说明 Day 4 单次 tool calling
   配合改良 prompt 在当前题集上已经接近上限 —— Agent 是"保险机制"，不是常规
   路径。**对评测分数有用，但没法用 baseline 数据讲"自修复救回了 X 题"的故事**。
2. **L5-03 比 Day 4 多调了一次 schema_lookup**（核查 MAKT/MARA），但没出现 Day 4
   那种 EKKO.NETWR 列名幻觉 —— 这次直接给出正确的 `JOIN VBAK + JOIN EKKO/EKPO`
   的 UNION ALL 写法。LLM 行为有运行间方差，不是 Agent 的功劳，但额外的 tool
   使用印证了"prompt 里强调字段不确定要先 lookup"的提示是生效的。
3. **修复链路独立验证**：`scripts/agent_repair_demo.py` 用 fault-injection 风格
   故意构造一条 `SELECT SUM(EKKO.NETWR) ...` 错误 SQL，让 Agent 走修复分支。
   实测结果：
   - 真实 SQLite 错误：`no such column: EKKO.NETWR`
   - Agent 自动调 `schema_lookup(["EKKO", "EKPO"])` 核对字段
   - 修复后 SQL：`JOIN EKPO ON EKKO.EBELN = EKPO.EBELN` 用 `SUM(EKPO.MENGE * EKPO.NETPR)`
   - 执行返回销售 13.1 亿 vs 采购 8.1 亿，**完整解决了 Day 3 baseline 里
     L5-03 列名幻觉的根本问题**
4. **延迟反而降了 24%**：Day 4 一些题 output_tokens 偏高（LLM 自言自语），Day 6
   的 build_repair_messages / system prompt 没动结构，主要差异在 LLM 内部
   采样波动。这条不算 Day 6 的"功劳"，只是说明当前架构在采样波动下依然稳定。

**Day 6 实现的工程要点：**

- LangGraph 1.x StateGraph，三节点（generate / execute / reflect）+ 一条
  条件边，约 200 行 Python，**完全复用 Day 4 的 generate_sql_with_tools 不重写**
- 自修复触发条件只看 SQLite 报错；"SQL 跑通但 row_count=0"暂时不触发，避免
  Agent 把"业务真没数据"误改成"放宽过滤条件"
- 每次失败都把 (sql, error) 拼进下一轮的 user message，让 LLM 看到**完整失败
  轨迹**（不只最近一次），避免反复犯同样的错
- attempt > 0 时仍保留 schema_lookup tool —— Day 4 经验：列名幻觉的标准
  补救动作就是先调 schema_lookup 核字段

**这两组基线锚定后续三段提升：**
- Day 4-5：input tokens 1117 → ≤500（Function Calling 按需召表 + 字段描述压缩）
- Day 6-8：自修复 Agent 把 L5-03 这类列名幻觉/JOIN 错的 case 救回来；在 Day 9 扩充的
  难题集上把成功率从 X% 拉到 ≥85%
- Day 9-11：扩充刁难题集 + 4 模型对比，画"准确率 × 成本"四象限

## Day 12（提前）Streamlit Web UI

把 CLI 的链路 1:1 搬到浏览器，**核心目标是把 Agent 中间过程肉眼可见**。

启动：
```bash
.venv\Scripts\python.exe -m streamlit run web/app.py
# 浏览器打开 http://localhost:8501
```

界面分两部分：

- **左侧侧边栏**：RAG / Tools / Agent 三档开关 + top-K / 自修复次数滑杆 + 数据库就绪状态。
  现场演示时可以一档一档关掉，让面试官看不同模式下的对比。
- **主区域对话流**：
  - 🔍 *Schema RAG 召回*：top-K 表名 + 余弦相似度，st.metric 横排
  - 🤖 *LangGraph Agent*：每次 attempt 的 PASS/FAIL 状态、错误信息、SQL（可展开）
  - 🔧 *Tool calls*：每次 schema_lookup 的参数
  - 📜 最终 SQL（语法高亮）+ token / 延迟元信息
  - 📊 查询结果表（pandas-style，自动列宽）
  - 💬 **业务解读流式渲染**：`chat_stream_iter` 生成器直吐 `st.write_stream`，
    无需 thread/queue 桥接，无需 sleep（Web 端 chunk 渲染本就比 CLI 快，
    再加 sleep 反而显得卡）

技术要点：
1. **`src/llm.py` 新增 `chat_stream_iter(messages, ..., on_done=callback)`**：
   `yield` 每个 token chunk，最后通过 `on_done` 回调一次性回传完整 usage / latency
   （这两个只在 stream 的最后一个 chunk 出现，调用方拿不到的话日志层会断）
2. **`@st.cache_resource` 预热 BGE 向量模型**：首次启动 25s 加载模型，
   缓存后后续 rerun 0ms。chroma client / collection 同样缓存
3. **复用 `run_agent()` 不重写链路**：UI 只是消费层，不再实现一遍 Day 3-6
   的业务逻辑。后续 Day 7-8 Agent 升级 / Day 9 评测扩充自动反映到 UI
4. **session_state 持久化对话历史**：每条历史 assistant 消息存的是结果 dict
   （SQL / 结果行 / agent meta / 解读文本），rerun 时**纯重渲染不重跑 LLM**

## Day 9 刁难题集（25 道新题，难题集才是真考场）

**为什么扩题**：Day 2 的 15 道老题在 Day 6 Agent 上已经 100% 通过，
自修复一次都没触发。**baseline 数据讲不出"自修复救回 X 题"的故事**，
必须扩出"足以让 Agent 翻车"的难题集，才能展示 Agent 的真实价值。

**25 道新题分 5 类**（[`eval/test_cases.json`](eval/test_cases.json) D9-01 ~ D9-25）：

| 类别 | 题量 | 设计意图 |
|---|---|---|
| A. 列名/字段易幻觉 | 4 | 复刻 L5-03 的 `EKKO.NETWR` 幻觉、`WRBTR/DMBTR` 选错 —— 验证 schema_lookup 价值 |
| B. 隐式过滤 / 业务常识推断 | 5 | "大客户"、"滞销物料"、"僵尸供应商" —— LLM 需自主选阈值 |
| C. 跨年度对比 | 5 | "同比增长"、"环比"、"Q1 vs Q4" —— L5-03 经验延伸，易语义不完整 |
| D. 多表深 JOIN（3-4 表） | 5 | LFA1+EKKO+EKPO+MARA 这种四表组合，容易漏 JOIN |
| E. 业务诱导 / 语义陷阱 | 6 | 数据校验、关联规则、隐式排除测试账号等 |

### Day 9 baseline 对比（40 题 = 老 15 + 新 25）

**两种模式跑同一套 40 题**：[`baseline_day9_rag.json`](eval/baseline_day9_rag.json) vs
[`baseline_day9_agent.json`](eval/baseline_day9_agent.json)

| 指标 | Day 3 模式（RAG-only） | Day 6 模式（RAG + Tools + Self-repair） | Δ |
|---|---|---|---|
| **40 题总通过** | 35/40 = **87.5%** | **39/40 = 97.5%** | **+10 pp** |
| └─ 老 15 题 | 15/15 = 100% | 15/15 = 100% | 持平（饱和）|
| └─ **新 25 道刁难题** | 20/25 = **80%** | **24/25 = 96%** | **+16 pp** |
| L4 通过率 | 9/11 = 81.8% | **11/11 = 100%** | +18 pp |
| L5 通过率 | 17/20 = 85% | 19/20 = 95% | +10 pp |
| 平均 input tokens | 1244 | 3310 | +2.7× |
| 平均 output tokens | 109 | 280 | +2.5× |
| 平均端到端延迟 | 2106 ms | 4224 ms | +2× |
| Tool 使用率 | 0% | **60%（24/40）** | — |
| **自修复触发** | 0 | **2 次（1 救回 / 1 失败）** | — |

### Day 9 核心战果：自修复在难题上**真的触发并救回了一题**

**D9-13「对比 2026 Q1 和 2025 Q4 销售订单数量」**（Day 9 自修复成功的代表 case）：
- **attempt 0** SQL：写了两个 SELECT 用 `;` 分隔 → SQLite 报错
  `You can only execute one statement at a time.`
- **attempt 1** SQL：Agent 看到错误后**重写为 UNION ALL** →
  `SELECT '2025-Q4', COUNT(...) UNION ALL SELECT '2026-Q1', COUNT(...)` → 2 行 PASS
- 这是 Day 1 → Day 6 的最终闭环：**Agent 在真实未见过的题上自主修复并通过**

**Agent 救回 Day 3 RAG-only 失败的 5 道题**：
- `L4-02`（上月销售排名）—— Agent 让时间过滤更稳
- `L5-03`（对比销售 vs 采购）—— Agent 调 `schema_lookup(["EKPO","MAKT"])` 核字段
- `D9-05`（大客户）—— Agent 补 JOIN KNA1
- `D9-08`（长尾客户）—— Agent 用窗口函数
- `D9-16`（供应商-物料类型）—— Agent 调 `schema_lookup(["LFA1"])` 补全 4 表 JOIN

### Day 9 唯一一道硬骨头：D9-11 反思节点的预定靶子

**D9-11「今年每月销售额的环比增长率」**：RAG-only 模式下 LLM 一次写对（用 LAG 窗口），
但**在 Agent 模式下 3 次 attempt 全报 `incomplete input`**。检查 final SQL 发现：

```sql
WITH monthly_sales AS (
  SELECT STRFTIME('%Y-%m', ERDAT) AS 月份, SUM(NETWR) AS 月销售额
  FROM VBAK
  WHERE ERDAT >= '2026-01-01' AND ERDAT < '2026-06-01'
  GROUP BY STRFTIME('%Y-%m', ERDAT)
)
SELECT     ← SQL 在这里被截断
```

**根因**：`src/main.py` 的 `_strip_code_fence` 在多行 WITH CTE + 中文别名 + tool calling
后续轮的复合场景下错误剥掉了 SQL 后半部分。**这条进 Day 7-8 的修复清单**，
同时也是"结果反思节点"的设计动机 —— 如果反思节点能识别"SQL 在 `SELECT` 后无内容"
就让 LLM 重生，本题应当被救回。

### Day 9 关键经验（写给未来的自己）

1. **难题集 = 反向工程产物**：先有 Day 6 跑出 100% 的事实，再设计能让 Agent 翻车
   的题，而不是凭空想"什么题难"。设计原则：触发 schema_lookup / 触发自修复 /
   触发语义不完整 三选一
2. **LLM 评测有运行间方差**：同一套题同一 LLM 跑两次，3-5 题会"飘"。这是 LLM 评测
   的固有问题，**不要拿单次跑作为绝对结论**。本项目用"修评测集 + 重跑"消除了
   2 次飘移，但 D9-11 这种深层 bug 飘了出来 —— 反而是真实弹药
3. **判分宁松勿严，分两次校准**：第一轮 25 题里有 4 道因评测集瑕疵 FAIL（包括
   `must_have_any` 漏了 LIMIT、`executable_required` 应放宽、`expected_tables`
   多写了一张表、数据失配题需要改）。**判分错误造成的 FAIL 比 LLM 真错危险得多**，
   会让你误改 Agent 来"修一个根本没错的输出"
4. **mock 数据 vs LLM 知识失配是真陷阱**：原 D9-17 问"销售订单未在财务凭证出现"，
   SAP 真实链路是 VBAK → VBRK → BKPF，但 mock 没建 VBRK/VBRP 开票表，LLM 调
   schema_lookup 6 次找不到，最终输出空 SQL。**重设计为 SD 内三表 JOIN 题**
   （按 MTART 物料类型分组销售排名）规避数据失配

## Day 7-8 Agent 架构升级：修 SQL 提取层 bug + 反思节点

**Day 9 baseline 暴露的两个 Day 7-8 靶子：**

1. **核心 bug**：D9-11「今年每月销售额环比增长率」连续 3 次 attempt 全报
   `incomplete input`，根因是 [src/main.py:58](src/main.py#L58) 的
   `_strip_code_fence` 把"以中文别名开头的字段列表行"（如 `  月份,`
   `  月销售额,`）误判为话术段落起点，把主 SELECT 后的整段砍掉。
2. **架构缺口**：reflect 节点是空壳子（只 `attempt += 1`），LLM 反复犯
   同样错时没有任何机制让它跳出循环。

### 修复 1：`_strip_code_fence` 改用白名单话术 trigger 判定

把"任何中文行都当作话术起点"的启发式（原 [src/main.py:84-96](src/main.py#L84-L96)）改成
**只在行内含明显话术 trigger 词时才截断**：

```python
_PROSE_LINE_TRIGGER = re.compile(
    "[。！？：；]|"
    "根据|首先|然后|接下来|现在|因此|所以|"
    "这条|这个|这是|这就|这里|上面|下面|上述|由于|"
    "等等|不过|另外|需要|应该|修正|重新|"
    "注意|说明|解释|总结|综上|至此|完成|"
    "让我|思考|分析|理解|意图|查询会|查询的|建议|推荐"
)
```

效果：
- `  月份,` `  月销售额,` ← 不含 trigger，**保留**（D9-11 救回）
- `这条 SQL 计算了月销售额。` ← 含"这条"+"。"，**截断**
- `等等，需要 JOIN VBAP...` ← 含"等等"，**截断**（Day 4 经验的 self-correction 场景仍然处理）

12 条单元测试覆盖（见 [tests/test_strip_code_fence.py](tests/test_strip_code_fence.py)）：
含 D9-11 原始 LLM 输出复现 case、中文别名 / WITH CTE / 围栏 / SQL 后跟话术等 case。

### 修复 2：reflect 节点加重复错误检测

新增 `_normalize_error()` 把 SQLite 错误归一到签名（`no_such_column:EKKO.NETWR`、
`incomplete_input`、`multiple_statements` 等）。reflect 节点比较最近两次 attempt
的签名是否一致，相同则 `repeat_count += 1`，下一轮 `_generate` 把 `repeat_count`
透传给 [src/prompts.py](src/prompts.py) `build_repair_messages`，在 user message
顶部追加 `REPEAT_ERROR_WARNING`：

```
⚠️ 重复错误警告：你已经连续 N 次遇到了同样的 SQLite 错误 "incomplete_input"。
继续重复同一思路只会再次失败。本轮必须：
1. 换一个根本不同的写法（如：拆掉 CTE、把窗口函数换成子查询自连接、改 JOIN 方向）
2. 如果是 "no such column" / "no such table"，先调 schema_lookup 核字段
3. 如果是 "incomplete input"，确保整段 SQL 写完整 + 没有截断
```

设计要点：
- **签名区分列名**：`no such column: EKKO.NETWR` 和 `no such column: EKKO.WAERS`
  签名不同 —— 说明 LLM 在尝试不同方向，**不算重复**，repeat_count 归零
- **签名忽略库内具体行号 / SQL 文本**：只看错误**类别**，对"反复犯同一类错"敏感
- 13 条单元测试覆盖（见 [tests/test_reflect_node.py](tests/test_reflect_node.py)）

### 修复 3：max_repair_attempts 默认 2 → 3

配合反思节点：第 1 次失败正常修，第 2 次失败 reflect 标记 repeat_count=1，
第 3 次 attempt 收到 REPEAT_ERROR_WARNING 强制换思路。Day 9 baseline 上
D9-13 是首错二对的真实成功 case，3 次预算足够覆盖此类长尾。

### Day 7 端到端基线（DeepSeek-Chat / RAG top-5 + Tools + Agent / 2026-05-21）

| 指标 | Day 9 baseline | **Day 7-8** | 变化 |
|---|---|---|---|
| 40 题总通过 | 39/40 = 97.5% | **40/40 = 100%** | **+2.5pp，D9-11 救回** |
| └─ 老 15 题 | 100% | 100% | 持平 |
| └─ 新 25 道刁难题 | 24/25 = 96% | **25/25 = 100%** | **+4pp** |
| L4 通过率 | 100% | 100% | 持平 |
| L5 通过率 | 19/20 = 95% | **20/20 = 100%** | +5pp |
| 平均 input tokens | 3310 | 3010 | **-9%** |
| 平均 output tokens | 280 | 238 | -15% |
| 平均端到端延迟 | 4224 ms | 4715 ms | +12% |
| Tool 使用率 | 60% | 52.5% | -7.5pp（LLM 更确信少调） |
| 自修复触发 | 2 次 | 0 次 | bug 修后没题需要走自修复 |

**Day 7-8 重要观察 —— 工程上的好结果，故事上的"问题"再次出现：**

1. **bug 修了之后自修复 0 次触发**。这跟 Day 6 第一次全通过时是同款现象 ——
   Agent 是保险机制不是常规路径。**反思节点的价值要靠单元测试 + 架构图证明，
   不能靠 baseline 数字证明**。13 个 reflect 节点测试 + 12 个 _strip_code_fence
   测试构成机制级证据。
2. **D9-11 在新规则下首次 attempt 即过**（attempt=1，独立验证 [eval/baseline_day7_smoke.json](eval/baseline_day7_smoke.json)）。
   说明 LLM 本身有能力写对窗口函数 SQL，只是 SQL 提取层把答案砍了。
   **业务能力没问题，是工程链路 bug** —— 这条经验对未来 Eval 失败题
   归因很有用：先核字符串处理层，再怀疑 LLM。
3. **input token -9% / output -15%**：跟 LLM 运行间方差有关，但也说明
   反思节点引入的 prompt 改动（多一行 incomplete input 诊断指导 +
   重复错误警告模板）没有显著拖累 token 体积。

**Day 7-8 没解决的（留给 Day 10-11 之后）：**
- 反思节点的"语义层失败检测"还没做（row_count=0 是否触发反思 / 结果反喂 LLM
  判断完整性）。在 Day 9 经验里被列为高风险（容易把"业务真没数据"误改成
  "放宽过滤"），暂时不实施
- 可选 plan 节点（窗口函数 / 同环比类先拆步骤）：本轮 LLM 在 D9-10/11/14
  这类题上已经能直接写对，plan 节点不刚需，留作进一步打磨

## Day 13-14 Docker 一键部署

把整套链路（BGE 中文 embedding 模型 + SQLite mock 数据 + Chroma 索引 + Streamlit UI）
打成单个 Docker 镜像，国内云服务器 `docker compose up -d` 一条命令起服务。

### 设计要点

1. **基础镜像 `python:3.12-slim-bookworm`**（3.14 太新部分 wheel 没适配；3.12 与
   langgraph 1.x / sentence-transformers 5.x / chromadb 1.x 全兼容）
2. **torch 显式装 CPU 版**（PyPI 默认 GPU 版 ~2GB，CPU 版仅 ~200MB，本项目用不到 GPU）
3. **构建期预热**：`docker build` 时执行三件事，让运行期即开即用：
   - 预下载 BGE-small-zh-v1.5 模型到镜像里的 HF 缓存（运行时无需外网）
   - 跑 `data.seed_data` 生成 SQLite mock 数据（Faker seed=42 可复现）
   - 跑 `scripts.build_index` 构建 Chroma 向量索引
4. **`.env` 不进镜像**：通过 `--env-file` / `docker-compose env_file` 运行时注入
   `DEEPSEEK_API_KEY` + `APP_PASSWORD`
5. **密码门**（`src/config.py` 的 `APP_PASSWORD`）：公网部署时前置一个密码输入页，
   防止 DeepSeek 额度被刷；本地开发留空即跳过校验
6. **非 root 用户跑 Streamlit**（容器安全最佳实践）
7. **健康检查**：`/_stcore/health` 端点失败自动重启

### 服务器部署步骤（国内云，4 核 4G 推荐）

```bash
# 1. 服务器上装 Docker（Ubuntu/Debian 示例）
curl -fsSL https://get.docker.com | sh
sudo systemctl enable --now docker

# 2. 把代码传上去（任选一种）
git clone <your-repo-url> my_sap_project && cd my_sap_project
# 或本地打 tar 包 scp 上去

# 3. 准备环境变量
cp .env.example .env
vi .env       # 填 DEEPSEEK_API_KEY 和 APP_PASSWORD（建议都改）

# 4. 构建并启动（首次构建 ~10-15 分钟：torch + sentence-transformers + 模型预下载）
docker compose up -d --build

# 5. 看日志确认启动成功
docker compose logs -f sap-copilot
# 看到 "You can now view your Streamlit app in your browser." 即就绪

# 6. 开放云厂商安全组 8501 端口（阿里云/腾讯云控制台），浏览器开
# http://<服务器公网 IP>:8501，输入 APP_PASSWORD 进入
```

### 常用运维命令

```bash
docker compose ps                    # 看容器状态 + 健康检查结果
docker compose logs --tail 100       # 看最近日志
docker compose restart sap-copilot   # 重启服务
docker compose down                  # 停止并删除容器（镜像保留）
docker compose up -d --build         # 改了代码后重新构建并启动
docker image prune -a                # 清理旧镜像（首次构建后空间会涨）

# 进容器调试
docker compose exec sap-copilot bash
# 容器内：python -m eval.run_eval --case D9-11 等命令可直接跑
```

### 镜像大小预估

| 层 | 大小 |
|---|---|
| python:3.12-slim 基础 | ~150 MB |
| torch CPU + sentence-transformers | ~600 MB |
| chromadb + 依赖 | ~150 MB |
| streamlit + pandas + pyarrow | ~250 MB |
| BGE 模型 | ~95 MB |
| 应用代码 + SQLite + Chroma | ~10 MB |
| **合计** | **~1.2-1.5 GB** |

4G 内存服务器跑这个容器毫无压力：BGE 模型常驻 ~400MB，Chroma 100MB，Streamlit 300MB，
峰值约 1GB。

### 安全注意

- `.env` **不会**进镜像（`.dockerignore` 显式排除）；如果你 push 镜像到公开 registry，
  仍然要确保 `.env` 不在 build context 里
- `APP_PASSWORD` 留空 = 全公网无密码访问，**不要在生产环境这么干**；演示用至少设个
  16 位随机串
- 如果担心被刷 API，可在 [src/llm.py](src/llm.py) 加 daily token 上限：超过就拒
  绝调用 LLM，回退到一个友好提示

## 简历金句（持续回填实际数字）

- SAP 10 张核心表场景下，Schema RAG top-5 召回率 **86.7%**，avg recall **94.4%**
- Schema RAG 上线后 input tokens 从 **1819 → 1117（-39%）**，端到端延迟 **2046 → 2006 ms**
- 引入 OpenAI Function Calling 协议下的 `schema_lookup` 工具，对 RAG 召回缺表
  （如 MAKT 物料中文描述）实现 LLM **主动兜底召回**，把表召回失分类问题
  （L4-01/L4-03）从 FAIL 救回 PASS
- 引入 LangGraph SQL 自修复 Agent（generate → execute → reflect 三节点状态机），
  端到端通过率从 14/15 → **15/15 = 100%**；自修复链路独立验证：真实 SQLite 报错
  `no such column: EKKO.NETWR` 后，Agent 自动调 schema_lookup 核字段 → 修正
  为 `JOIN EKPO + SUM(NETPR*MENGE)`，二次执行成功
- 自建 40 题 SAP text-to-SQL eval set（覆盖 SD/MM/FI 三模块，含 25 道刁难题），
  Agent 模式在难题集上从 **80% → 96%（+16pp）**，自修复真实触发 2 次救回 1 题；
  暴露并定位 SQL 提取层 bug（D9-11 多行 WITH CTE 被错误截断），进 Day 7-8 修复清单
- Day 7-8 修复 SQL 提取层 bug（中文别名启发式误截）+ Agent 反思节点加重复错误
  检测：40 题通过率 **39/40 → 40/40 = 100%**，L5 难题 95% → **100%**；新增
  25 条单元测试覆盖 _strip_code_fence + reflect 节点机制，把"链路稳健性"
  从 baseline 数字落到代码级证据
- 单镜像 Docker 一键部署（~1.5GB，BGE 模型 + SQLite + Chroma 索引全部构建期烤入，
  运行期无外网依赖），密码门 + 非 root 用户 + healthcheck 三件套，4 核 4G 国内云
  服务器 `docker compose up -d` 一条命令上线
- 100 条业务问题 eval set，对比 4 个主流 LLM，最终模型成本是 GPT-4 的 __%
