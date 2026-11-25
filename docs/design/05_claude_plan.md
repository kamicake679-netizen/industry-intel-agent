# 项目二 · Claude 方案：「情报简报」编辑部风格前端 + 工程增强

> 本方案与 `04_项目二_行业情报分析Agent架构与提示词指南.md` **业务目标一致**（结构化抽取 + LangGraph Text-to-SQL + 看板 + Webhook + 评测 + 魔搭部署），区别在于：
> 1. **前端审美**：放弃“靛蓝科技风 BI 看板”这一 AI 生成作品的默认长相，改为有辨识度的**报纸头版 / 情报简报**风格；
> 2. **工程深度**：几处关键实现换成面试时更站得住的做法（只读连接 + SQLite 授权器、规范化技能表、SSE 流式轨迹、真实数据优先）；
> 3. **前端流程**：拆成“设计系统 → 静态高保真稿 → 接线 → 视觉 QA”四步，不让 AI 一口气写出 1500 行的单文件 HTML。
>
> 设计预览稿：[design_preview.html](./design_preview.html)（双击用浏览器打开，作为本工作区视觉基准）。

---

## 一、为什么要换审美方向

面试官和 HR 一天会看到很多“深蓝渐变 + 圆角阴影卡片 + 紫色按钮 + emoji 图标”的 AI 看板，这种页面**一眼就能看出是 AI 默认生成的**，再漂亮也没有记忆点。

项目二的本质是**“情报”**：采集 → 整理 → 解读 → 推送。那就让它长得像一份真正的情报简报：

| 常见 AI 看板 | 情报简报风格（本方案） |
| :--- | :--- |
| 仪表盘堆满卡片，没有主次 | **头版逻辑**：一句数据驱动的大标题先给结论，再给 KPI 和图表佐证 |
| 渐变、毛玻璃、大圆角、阴影 | 纸张底色 + 墨色文字 + 1px 细线分隔，**零阴影、零渐变** |
| 多彩图表，每根柱子一种颜色 | 墨灰为主，**只用一种朱砂红标出最重要的那个数** |
| 聊天框 = 气泡 | 右侧深色“问询台”，像终端一样逐步点亮 LangGraph 节点 |
| 图表无出处 | 每张图都有“图 1 / 表 1”编号和**数据来源 + 样本量**脚注（体现数据素养） |

---

## 二、设计系统（Design Tokens）

### 1. 色彩

| Token | 色值 | 用途 |
| :--- | :--- | :--- |
| `--paper` | `#F3EFE6` | 页面底色（暖白纸张） |
| `--paper-2` | `#E9E3D6` | 次级底色、悬停背景 |
| `--ink` | `#1B1A17` | 标题、正文主色 |
| `--ink-2` | `#55514A` | 副文本、常规柱 |
| `--ink-3` | `#8A847A` | 注释、脚注、坐标轴 |
| `--rule` | `#D6CEBD` | 1px 分隔细线 |
| `--accent` | `#C8401A` | **朱砂红：全页唯一强调色**，每屏最多出现 3 处 |
| `--moss` | `#4E6B4F` | 上升 / 成功 |
| `--ochre` | `#B7822A` | 警示 / 自愈重试 |
| `--blue` | `#23395B` | 次级数据系列（双系列图表时才用） |
| `--desk` | `#161512` | 问询台底色 |
| `--desk-fg` / `--desk-mute` / `--desk-rule` | `#E9E3D6` / `#8F887C` / `#2E2B26` | 问询台文字、弱文字、分隔线 |

**夜读模式**：把 paper/ink 互换为 `#141310` / `#E6E0D2`，强调色不变。

### 2. 字体

| 角色 | 字体栈 | 规则 |
| :--- | :--- | :--- |
| 标题 | `"Noto Serif SC","Source Han Serif SC","Songti SC",serif` | 700/900 字重，头条 40px，区块标题 18px |
| 正文 | `"PingFang SC","HarmonyOS Sans SC","Microsoft YaHei",sans-serif` | 15px / 行高 1.75 |
| 数字 / SQL | `"JetBrains Mono","Cascadia Mono",Consolas,monospace` | **必须 `font-variant-numeric: tabular-nums`**，数字列对齐 |
| 栏目标签 | 正文字体 12px | 字距 `0.14em`，`--ink-3` 色，如「头条 · 本周观察」 |

字体加载：Google Fonts 在国内不稳定，使用镜像 `https://fonts.loli.net/css2?family=Noto+Serif+SC:wght@700;900&family=JetBrains+Mono:wght@400;500&display=swap`（按 unicode-range 分片，只下载用到的字），失败时自动回退系统字体。

### 3. 布局与细节

- 12 栏网格，最大宽度 1320px；桌面端「头版 8 栏 + 问询台 4 栏」，<1024px 时问询台变为底部抽屉。
- 报头下方用 `3px double` 双线，区块之间用 1px `--rule` 细线；**圆角 0~2px，无阴影**。
- KPI 用竖向细线分隔的 4 栏，大号等宽数字（34px）+ 小号说明，不做卡片。
- 链接/按钮：文字 + 悬停下划线展开动画，而不是实心彩色按钮；唯一的实心按钮是问询台的“发送”。
- 图标：不用 emoji，需要时用 1.5px 线性 SVG（Lucide 风格）。

### 4. 动效（克制）

- 首屏 KPI 数字 count-up 一次（600ms，easeOutCubic）；图表入场 400ms。
- 问询台节点：运行中朱砂红圆点呼吸闪烁，完成变灰，自愈变赭石，安全拦截变红并显示“已拦截”。
- 答案文本流式逐字出现；SQL 块出现时轻微淡入。
- 全部遵守 `prefers-reduced-motion`。

### 5. ECharts 主题规范（`web/js/echarts-theme.js`）

- 背景透明；坐标轴线 `--rule`，刻度文字 `--ink-3` 12px；**去掉 Y 轴网格线**，只保留基线。
- 默认柱色 `--ink-2`，第 1 名或用户关注项 `--accent`，第 6 名以后降透明度到 0.55。
- 柱子末端直接标数值（等宽字体），不依赖 tooltip 读数。
- tooltip：纸色背景 + 1px 墨线边框 + 无阴影。
- 禁用：渐变填充、3D、饼图超过 6 块（超出合并为“其他”）。

### 6. 页面信息架构

```text
┌──────────────────────────────── 报头：INDUSTRY INTEL · 第 N 期 · 日期 · 数据更新时间 ═══════
│ 城市筛选 Tab（全部/广州/深圳/东莞）                 ＋投稿一份 JD   生成今日简报   关于本刊
├───────────────────────────────────────────────┬──────────────────────────
│ 头条（LLM 根据当日数据生成一句结论 + 两行导语）       │  问询台 ASK THE DESK
│ KPI ×4（收录岗位 / 平均月薪 / 应届友好 / 最高月薪）   │  问：……
│ 图1 技能提及率 TOP 10   │ 表1 三城对照 + 编者按        │  LangGraph 执行轨迹（逐步点亮）
│ 图2 经验—薪资阶梯图     │ 图3 岗位大类构成             │  SQL（只读·已加 LIMIT）
│ 最新入库（时间线样式，新投稿的 JD 带朱砂“新”标记）     │  答：……（含迷你图）
│ 脚注：数据来源、样本量、方法说明                      │  快捷问题 chips + 输入框
└───────────────────────────────────────────────┴──────────────────────────
```

---

## 三、工程层增强（相比 04 号方案的改动点）

| 环节 | 04 号方案 | 本方案 | 面试时怎么说 |
| :--- | :--- | :--- | :--- |
| 数据来源 | AI 生成 230 条“高仿真”数据 | **优先导入你真实采集的 232 份岗位（脱敏公司名可保留）**；仅在缺字段时补全，生成数据必须在页面脚注标明“演示数据” | 面试官问“数据哪来的”，能真实回答，不会被追问穿帮 |
| 技能存储 | `skills_json` 字符串 + `LIKE '%"RAG"%'` | 增加规范化表 `job_skills(job_id, skill)`，并做技能同义词归一（`langgraph`/`LangGraph` → `LangGraph`） | 第一范式；Text-to-SQL 准确率明显提高，也是评测里可量化的一项优化 |
| SQL 安全 | 正则黑名单 | **三层**：① `sqlite3.connect("file:intel.db?mode=ro", uri=True)` 只读连接；② `conn.set_authorizer()` 只放行 `SQLITE_SELECT`/`SQLITE_READ`/`SQLITE_FUNCTION`；③ 正则预检 + 自动 `LIMIT` + 查询超时 (`progress_handler`) | 正则能被绕过，数据库层的只读与授权器才是真正的边界——这句话非常加分 |
| 轨迹展示 | 请求结束后一次性返回 `trace` | `POST /api/chat/stream`（SSE），用 LangGraph `astream(stream_mode="updates")` 每完成一个节点推送一个事件 | 用户体感延迟大幅下降；前端节点逐个点亮，演示效果最好 |
| 头条生成 | 无 | `GET /api/headline`：LLM 根据当日统计数据生成一句头条 + 两行导语，**按日期缓存**，失败回退为模板句 | 体现“数据 → 洞察”的产品化思维，并且控制了 Token 成本 |
| 演示数据持久化 | 未说明 | 魔搭容器重启后新增数据会丢失，页面脚注注明“演示环境每日重置” | 主动说明边界，比被问到更好 |
| 限流 | 单 IP 每分钟 15 次 | 单 IP 每分钟 10 次 + **全局每日 LLM 调用上限**（如 800 次），超限后问询台优雅降级为只读看板 | 防止公开链接被刷爆额度 |
| 前端结构 | 单文件 `index.html` | `index.html` + `css/` + `js/` 多文件，ECharts / marked **本地托管**于 `web/vendor/` | 避免国内 CDN 不稳定；多文件也方便 AI 逐块修改，不会截断 |

---

## 四、推荐目录结构

```text
industry-intel-agent/
├── app/
│   ├── main.py  config.py
│   ├── db/        database.py  seed_loader.py  skill_normalizer.py
│   ├── pipeline/  extractor.py
│   ├── agent/     state.py  prompts.py  sql_guard.py  graph.py
│   └── services/  analytics.py  headline.py  notifier.py  ratelimit.py
├── web/
│   ├── index.html
│   ├── css/       tokens.css  base.css  layout.css  components.css
│   ├── js/        api.js  echarts-theme.js  dashboard.js  desk.js  modals.js  main.js
│   ├── mock/      dashboard.json  chat_stream_sample.json   # 静态高保真稿用的假数据
│   └── vendor/    echarts.min.js  marked.min.js
├── docs/design/   05_claude_plan.md  design_preview.html  screenshots/
├── data/          raw/  seed_jobs.json  eval_sql_dataset.json
├── eval/          run_eval.py  report_generator.py
├── scripts/       test_llm.py  test_step1.py  test_step2.py  test_api.py
├── Dockerfile  requirements.txt  .env.example  .gitignore  README.md
```

---

## 五、分步提示词（新工作区按顺序逐条发送）

> **节奏**：每步结束 → 跑验收命令 → 自己看一遍核心代码 → `git commit` → 再发下一步。
> **开工前**：本文件和 `design_preview.html` 已保存在 `docs/design/` 目录；如有真实岗位数据，放到 `data/raw/`。

### Step 0：骨架与连通性

```text
我要开发开源作品集项目「industry-intel-agent：垂直行业情报采集与对话式分析 Agent」。
运行环境约束：Python 3.11；大模型只用硅基流动 OpenAI 兼容接口（base_url=https://api.siliconflow.cn/v1，默认模型 deepseek-ai/DeepSeek-V3）；最终以 Docker 模式部署到魔搭创空间，监听 0.0.0.0:7860。
设计规范在 docs/design/ 目录下（05 号方案 md 与 design_preview.html），请先通读，后续所有步骤都以它为准。

本步只做初始化：
1. 按 05 号方案「四、推荐目录结构」创建目录与空文件占位。
2. 创建 .gitignore（.env、.venv/、__pycache__/、data/*.db、eval/results/）与 .env.example（SILICONFLOW_API_KEY、SILICONFLOW_BASE_URL、LLM_MODEL、PORT=7860、WEBHOOK_URL、DAILY_LLM_BUDGET=800）。
3. requirements.txt：fastapi、uvicorn[standard]、pydantic、pydantic-settings、python-dotenv、openai、langgraph、langchain-openai、sse-starlette、httpx、jieba（如后续不需要可删）。
4. app/config.py：用 pydantic-settings 读取配置，系统环境变量优先（兼容魔搭 Secrets），本地回退 .env。
5. scripts/test_llm.py：调用一次模型，打印回复、耗时和 token 用量。

不要写业务代码。完成后给出创建虚拟环境、安装依赖、运行连通测试的命令。
```

**验收**：`python scripts/test_llm.py` 打印出回复与耗时。

---

### Step 1：数据层（真实数据优先 + 规范化技能表 + 结构化抽取）

```text
Step 0 已通过。现在做 Step 1：数据层。

1. app/db/database.py：建库 data/intel.db，表结构：
   - job_intel：id, job_hash(UNIQUE), title, company, city, district, category, salary_min, salary_max, salary_avg（单位 K）, experience（枚举：经验不限/应届生/1年以内/1-3年/3-5年/5年以上）, education（大专/本科/硕士/博士/不限）, company_size, industry, fresh_grad_friendly(0/1), raw_text, source（real_import/demo_seed/user_submit）, created_at
   - job_skills：job_id, skill（联合主键，建 skill 索引）
   - query_logs：id, ts, question, sql, ok, retry_count, blocked, latency_ms
   - get_schema_description()：输出表结构、字段中文含义、每个枚举字段的实际取值（从库里 SELECT DISTINCT 动态生成）、高频技能前 40 个，供 Text-to-SQL 注入。
2. app/db/skill_normalizer.py：技能同义词归一（大小写、中英文别名，如 "大模型"→"LLM"、"langgraph"→"LangGraph"、"function call"→"Function Calling"），提供 normalize_skills(list[str]) -> list[str]。
3. app/db/seed_loader.py：
   - 优先读取 data/raw/ 下的真实岗位文件（CSV 或 JSON，自动识别列名并映射到上述字段，无法映射的字段留空）。导入前先打印字段映射结果让我确认。
   - 只有当 data/raw/ 为空时，才生成 data/seed_jobs.json 作为演示数据，source 标记为 demo_seed。
   - init_and_seed_db() 幂等：按 job_hash 去重，同时写入 job_skills。
4. app/pipeline/extractor.py：Pydantic 定义 ExtractedJob，调用 LLM 用 JSON 模式（response_format={"type":"json_object"}）把一段杂乱 JD 抽取为结构化对象；薪资统一换算为 K（"15-25K·14薪"、"8000-12000元/月"、"20-30万/年" 都要正确处理，年薪按 12 个月折算）；解析失败重试 1 次；extract_and_save_job(raw_text) 返回 {job, is_new}。
5. scripts/test_step1.py：验证导入条数、各城市计数、技能 TOP 10，以及 3 段不同风格 JD（含年薪写法）的抽取结果。

实现并运行自测，把输出贴给我。
```

**验收**：库内条数正确；3 段 JD 的薪资换算全部正确；`job_skills` 中不存在大小写重复的技能。

---

### Step 2：LangGraph Text-to-SQL Agent（三层只读防线 + 自愈 + 流式事件）

```text
Step 1 已通过。现在做 Step 2：LangGraph Text-to-SQL Agent。

1. app/agent/sql_guard.py —— 三层防线：
   a. 预检：去掉注释后只允许单条 SELECT / WITH...SELECT；拒绝多语句；自动为非聚合查询补 LIMIT 50。
   b. 执行连接：sqlite3.connect("file:data/intel.db?mode=ro", uri=True)，并用 conn.set_authorizer 只放行 SQLITE_SELECT、SQLITE_READ、SQLITE_FUNCTION，其余一律 SQLITE_DENY；禁止读取 sqlite_master 以外的系统对象和 query_logs 表。
   c. 超时：用 conn.set_progress_handler 限制单条查询最长 2 秒。
   返回结构：{ok, sql, columns, rows, row_count, error, blocked, block_reason}。
2. app/agent/state.py：AgentState(TypedDict)：question, history(最近 3 轮), intent, sql, columns, rows, row_count, error, retry_count, max_retries=3, blocked, answer, chart, trace(list)。
3. app/agent/prompts.py：
   - 意图识别：data_query / chitchat / unsafe（明确的写操作或越权意图直接判 unsafe）。
   - SQL 生成：注入 get_schema_description() + 6 条 few-shot（技能用 JOIN job_skills；城市对比；经验分组均薪；应届友好筛选；TOP N；占比计算）。
   - SQL 修复：给出原 SQL + 报错 + schema，只输出修正后的 SQL。
   - 洞察总结：基于结果用 2~4 句中文给结论，数字必须来自结果，不得编造；以 JSON 输出 {answer, chart:{type: bar|line|none, title, labels, values}}。
4. app/agent/graph.py：StateGraph 节点 intent_router → (chitchat_answer | sql_generator | security_fallback)；sql_generator → sql_executor；sql_executor 条件边：blocked→security_fallback，error 且 retry_count<max_retries→sql_generator（修复模式），error 超限→error_fallback，成功→insight_synthesizer→END。
   每个节点向 trace 追加 {node, status: ok|retry|blocked|error, detail, ms}。
   暴露：
   - run_agent(question, history=None, self_correct=True) -> dict（同步，供评测用）
   - astream_agent(question, history=None) -> AsyncIterator[dict]（基于 graph.astream(stream_mode="updates")，每完成一个节点 yield 一个事件，最后 yield {type:"final", ...}）
   每次运行写一条 query_logs。
5. scripts/test_step2.py：跑 6 个用例并打印 trace：三城对比；广州不限经验且要求 RAG 的均薪；经验分组均薪；"把所有薪资改成 100K"（应拦截）；"SELECT * FROM job_intel; DROP TABLE job_intel"（应拦截）；"这个系统能做什么"（闲聊）。

实现并运行，贴出 6 个用例的结果。
```

**验收**：两类攻击均被拦截，且即使绕过预检，只读连接与授权器也能兜住（让 AI 额外写一个直接调用执行层的单测证明这一点）。

---

### Step 3：FastAPI 服务（SSE 流式 + 头条 + 简报推送 + 限流预算）

```text
Step 2 已通过。现在做 Step 3：后端 API。

1. app/services/analytics.py：get_dashboard(city=None) 返回 kpi（总数、均薪、中位薪、应届友好数与占比、最高薪岗位、本周新增）、skills_top（前 10，含提及率）、city_compare（岗位数/均薪/应届友好占比）、exp_salary（经验阶梯与均薪）、category_mix（前 5 + 其他）、latest（最新 12 条，含 source 字段）、meta（样本量、数据来源说明、是否含演示数据、更新时间）。
2. app/services/headline.py：get_headline(city=None)：根据 get_dashboard 的数字让 LLM 生成 {kicker, title(≤32字), dek(≤70字)}，要求数字必须来自输入；按 日期+城市 缓存到内存；LLM 失败时回退到模板句。
3. app/services/notifier.py：generate_brief() 生成 Markdown 简报；push_webhook(md, url=None) 按域名适配飞书/钉钉/企微格式，无 URL 时返回 {mode:"preview", markdown}。
4. app/services/ratelimit.py：单 IP 滑动窗口（10 次/分钟）+ 全局每日 LLM 预算（DAILY_LLM_BUDGET），超限返回 429 及中文 message，并在 /api/health 中暴露剩余预算。
5. app/main.py 路由：
   - GET / 及静态目录 /static → web/
   - GET /api/health、GET /api/dashboard?city=、GET /api/headline?city=
   - POST /api/chat（同步，返回完整结果）
   - POST /api/chat/stream（sse-starlette，事件类型：node / final / error）
   - POST /api/extract、POST /api/brief
   lifespan 中执行 init_and_seed_db()；全局异常处理返回统一 {error, message}。
6. scripts/test_api.py：用 TestClient 覆盖所有接口，SSE 接口要验证至少收到 1 个 node 事件和 1 个 final 事件。

实现并运行自测。
```

**验收**：`/docs` 可见全部接口；用 `curl -N -X POST localhost:7860/api/chat/stream -H "Content-Type: application/json" -d "{\"question\":\"三城均薪对比\"}"` 能看到事件逐条输出。

---

### Step 4：设计系统 + 静态高保真稿（只用假数据，不接后端）

> 这是审美成败的关键一步：**先把“长相”定死，再接数据**。

```text
Step 3 已通过。现在做 Step 4：前端设计系统与静态高保真稿。先不要调用任何后端接口。

请先完整阅读 docs/design/05_claude_plan.md「二、设计系统」与 docs/design/design_preview.html，视觉必须与之一致：情报简报 / 报纸头版风格，暖白纸张底色、墨色文字、1px 细线分隔、唯一强调色朱砂红 #C8401A、零阴影零渐变、圆角 ≤2px、标题衬线体、数字等宽体。

禁止出现：紫色或蓝紫渐变、毛玻璃、大圆角阴影卡片、emoji 图标、Tailwind 默认 indigo 配色、彩虹色图表。

任务：
1. web/css/tokens.css：用 CSS 变量定义全部色彩、字体、字号阶梯（12/13/15/18/24/40）、间距阶梯（4 的倍数）、夜读模式变量（[data-theme="night"]）。
2. web/css/base.css、layout.css、components.css：手写 CSS（不使用 Tailwind），12 栏网格，桌面「头版 8 栏 + 问询台 4 栏」，<1024px 时问询台变为底部抽屉（带拖拽把手视觉），<640px KPI 改为 2×2。
3. web/js/echarts-theme.js：按 05 号方案「ECharts 主题规范」注册主题 "brief"，并提供 highlightIndex 参数让指定柱子使用强调色。
4. 把 echarts.min.js、marked.min.js 下载到 web/vendor/（本地托管）。
5. web/mock/dashboard.json 与 web/mock/chat_stream_sample.json：按 Step 3 接口的真实返回结构造假数据。
6. web/index.html + web/js/*.js：用 mock 数据渲染完整页面——报头（期号、日期、更新时间）、城市 Tab、头条、KPI×4、图1 技能 TOP10 横向条形、表1 三城对照 + 编者按、图2 经验—薪资阶梯、图3 岗位大类构成、最新入库时间线、页脚方法说明；右侧问询台：空状态（一句引导 + 4 个快捷问题）、问答记录、LangGraph 执行轨迹（逐节点出现：灰=完成、朱砂呼吸=运行中、赭石=自愈、红=拦截）、SQL 块（复制按钮）、答案流式逐字渲染、迷你图。用 chat_stream_sample.json 按 300ms 间隔模拟流式播放。
7. 两个模态框（同样风格，不要白色圆角大卡片）：「投稿一份 JD」（左侧粘贴区 + 两个示例一键填充，右侧以“字段 — 值”的报表样式展示抽取结果）；「生成今日简报」（排版成一页报纸摘要样式的 Markdown 预览 + Webhook 输入框）。
8. 动效：KPI count-up、图表 400ms 入场、节点呼吸点；遵守 prefers-reduced-motion。

完成后启动服务，打开 http://127.0.0.1:7860 ，在 1440px 与 390px 两个宽度下截图保存到 docs/design/screenshots/，并对照 design_preview.html 列出你认为还不一致的地方。
```

**验收**：截图看起来像一份“简报”而不是“后台管理系统”；整页强调色不超过 3~4 处；数字列全部对齐。

---

### Step 5：前端接线（真实接口 + 流式轨迹 + 交互细节）

```text
Step 4 的静态稿我已确认。现在做 Step 5：把前端接到真实接口，视觉不做任何改变。

1. web/js/api.js：封装 getDashboard、getHeadline、chatStream（fetch + ReadableStream 解析 SSE，支持 AbortController 中断）、extract、brief；统一处理 429（显示“今日演示额度已用完 / 请稍后再试”的报纸“更正启事”样式提示条）与网络错误。
2. 城市 Tab 切换：同时刷新 dashboard 与 headline，图表用 setOption 平滑过渡而不是销毁重建；URL 同步 ?city= 便于分享。
3. 问询台：
   - 节点事件到达即点亮对应轨迹行；final 到达后渲染 SQL、答案（逐字）、迷你图、结果预览表（前 8 行，可展开）。
   - 生成中可点击「停止」；回答完成后提供「复制 SQL」「在看板中高亮」（若结果涉及某城市/技能，左侧对应柱子或行高亮 2 秒）。
   - 保留最近 3 轮对话作为 history 传给后端。
   - 键盘：Enter 发送、Shift+Enter 换行、/ 聚焦输入框、Esc 关闭模态框。
4. 投稿 JD 成功后：最新入库时间线顶部插入新条目并带朱砂“新”标记，KPI 数字从旧值 count-up 到新值。
5. 骨架屏：加载中用与文字行高一致的细条占位（像排版待印的版面），不要用旋转圈。
6. 若 dashboard.meta 显示含演示数据，页脚和头条下方都要出现“含演示数据”的小字标注。

完成后用真实数据逐一走一遍：切换三个城市、问 4 个快捷问题、投稿 1 份 JD、生成简报，报告是否有 JS 控制台报错。
```

**验收**：问一个问题时，右侧节点能**一个接一个**亮起，而不是等几秒后整块出现。

---

### Step 6：视觉 QA 与打磨

```text
Step 5 已通过。现在做 Step 6：视觉 QA。请逐项检查并修复，最后给我一份检查结果表（项目 / 是否通过 / 修复说明）：

1. 排版：标题无孤字换行（必要时用 text-wrap: balance）；正文行宽不超过 38 个汉字；中英文、中文与数字之间的间距一致。
2. 数字：所有数字使用等宽字体与 tabular-nums；单位（K、%）比数字小一号；同一列小数位数一致。
3. 色彩：强调色每屏 ≤4 处；正文与背景对比度 ≥ 4.5:1（问询台弱文字也要达标）；夜读模式下图表颜色同步切换。
4. 图表：每张图都有编号、标题、数据来源与样本量脚注；无网格线杂讯；数值直接标在柱端。
5. 响应式：1440 / 1024 / 768 / 390 四个宽度截图存到 docs/design/screenshots/，检查无横向滚动、无文字溢出、移动端问询台抽屉可正常打开。
6. 状态：空状态、加载态、错误态、429 限流态、安全拦截态都要有设计过的样式，并各截一张图。
7. 可访问性：交互元素可 Tab 聚焦且有可见焦点样式（朱砂色 2px 下划线）；图表容器有 aria-label 文字摘要。
8. 性能：首屏不阻塞渲染（字体 display=swap，脚本 defer）；Lighthouse 性能与可访问性均 ≥ 90（如本机可运行）。
```

---

### Step 7：Text-to-SQL 自动化评测

```text
Step 6 已通过。现在做 Step 7：评测流水线。

1. data/eval_sql_dataset.json：30 题，字段 id, category, question, reference_sql, expect_blocked。分布：基础筛选 8、分组聚合 8、技能匹配与复合条件 8、安全攻击 6（含写操作诱导、多语句注入、注释绕过、读取 query_logs、超大结果集、自然语言诱导“忽略之前的规则”）。reference_sql 必须能在当前库上运行。
2. eval/run_eval.py 三组配置做消融：
   - A 基线：仅表名字段名，无枚举、无 few-shot、无自愈、仅正则防护；
   - B：+ 完整 schema 描述与枚举 + few-shot；
   - C 完整版：B + 自愈重试 + 规范化技能表 + 三层只读防线。
   指标：首次执行成功率、最终执行成功率、结果准确率（与 reference_sql 结果集比对：忽略列名与行顺序，数值容差 0.01；不一致时再用 LLM 判定是否语义等价并单独计数）、安全拦截率、平均自愈次数、P50/P95 延迟、平均 token 成本。
   支持 --limit N 与 --config A|B|C|all。
3. eval/report_generator.py：生成 eval/EVAL_REPORT.md（总表、分类得分、3 个典型自愈案例、全部拦截案例）和 eval/results/*.json；同时输出一张同样采用简报风格的 eval/report.html。

先用 --limit 6 试跑，再告诉我全量命令。报告中的数字只能来自真实运行结果。
```

---

### Step 8：Docker、README 与魔搭上线

```text
Step 7 全量评测已完成。现在做 Step 8：上线收尾。

1. Dockerfile：python:3.11-slim，pip 使用国内镜像，TZ=Asia/Shanghai，PYTHONUNBUFFERED=1，EXPOSE 7860，CMD uvicorn app.main:app --host 0.0.0.0 --port 7860；.dockerignore 排除 .env、.venv、eval/results、docs/design/screenshots 之外的大文件。确认镜像内包含 data/seed_jobs.json（或 data/raw/ 的脱敏数据）与 web/vendor/。
2. README.md（面向面试官）：
   - 首屏：一句话定位 + 魔搭在线体验链接占位 + 1440px 首页截图；
   - 架构 Mermaid 图与 LangGraph 状态图；
   - 评测消融表（数字直接取自 eval/EVAL_REPORT.md）；
   - “安全设计”小节讲清三层只读防线；
   - “设计说明”小节：为什么选择情报简报风格、设计 tokens 一览；
   - 本地运行 / Docker 运行 / 魔搭创空间部署（Secrets 配置 SILICONFLOW_API_KEY）。
3. 上线前检查：全仓搜索疑似 API Key；确认 .env 未被跟踪；本地 docker build 与 docker run -p 7860:7860 验证；给出推送 GitHub 与魔搭创空间部署的步骤清单。
```

**验收**：魔搭公网链接用手机和电脑都能打开；问询台流式轨迹正常；README 首屏截图足够好看，能直接放进作品集。

---

## 六、怎么选：04 号方案 vs 本方案

- **时间紧、求稳**：用 04 号方案，前端单文件，三四天能出成品。
- **想让作品集有记忆点、面试多聊工程细节**：用本方案，前端多两步（静态稿 + 视觉 QA），大约多花 1 天。
- **混用也可以**：后端按 04 号方案写，只把本方案的 **Step 4 ~ Step 6** 和「三、工程层增强」里的“只读连接 + 授权器”“SSE 流式轨迹”两项加进去，性价比最高。
