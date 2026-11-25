# AGENTS.md — industry-intel-agent 项目开发与跨会话协作总纲

> **强制声明**：任何新会话进入本工作区开发时，**必须先完整阅读本文件**，并严格按照 [docs/design/05_claude_plan.md](docs/design/05_claude_plan.md) 与 [docs/design/design_preview.html](docs/design/design_preview.html) 中的 Claude 方案推进，**除非用户在后续对话中明确提出修改意见**。

---

## 一、项目目标与权威蓝图

- **项目名称**：`industry-intel-agent`（垂直行业情报采集与对话式分析 Agent）
- **核心能力**：真实岗位数据优先入库 + 规范化技能表 + LLM 结构化 JD 抽取 + LangGraph Text-to-SQL（三层只读防线 + SQL 自愈 + SSE 流式轨迹）+ 「情报简报」编辑部风格看板 + Webhook 简报推送 + Text-to-SQL 消融评测 + Docker / 魔搭创空间上线。
- **权威设计文档（不得擅自偏离）**：
  - 方案全文与分步指令：[docs/design/05_claude_plan.md](docs/design/05_claude_plan.md)
  - 视觉基准稿：[docs/design/design_preview.html](docs/design/design_preview.html)

---

## 二、核心架构与运行环境约束

### 1. 运行时与部署目标
- **目标生产 / Docker 环境**：`Python 3.11`（`python:3.11-slim`），以 Docker 模式部署至魔搭创空间，监听 `0.0.0.0:7860`。
- **本机已探测环境**：`Python 3.10.11`（`D:\Python\Python body\python.exe`）。代码须同时兼容 Python 3.10+ 与 3.11（避免 3.11 专属语法，以便本机与容器均无阻运行；如本机另装 3.11 可直接切换 `.venv`）。
- **大模型接入**：默认使用硅基流动 OpenAI 兼容接口（`SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1`，`LLM_MODEL=deepseek-ai/DeepSeek-V3`），所有模型与密钥均通过 `app/config.py` + 环境变量驱动，**禁止硬编码**单一厂商密钥或写死客户端地址，保持 OpenAI 兼容协议零锁定。

### 2. 环境变量约定（系统环境变量优先，兼容魔搭 Secrets，本地回退 `.env`）
完整模板见 [.env.example](.env.example)：
- `SILICONFLOW_API_KEY`：API 密钥（必填，严禁入库）
- `SILICONFLOW_BASE_URL`：接口基址，默认 `https://api.siliconflow.cn/v1`
- `LLM_MODEL`：默认模型，默认 `deepseek-ai/DeepSeek-V3`
- `PORT`：服务监听端口，默认 `7860`
- `WEBHOOK_URL`：飞书 / 钉钉 / 企微简报推送地址（可选，未配置时走预览模式）
- `DAILY_LLM_BUDGET`：全局每日 LLM 调用上限，默认 `800`

### 3. 工程增强红线（Claude 方案第三章）
- **数据来源**：优先读取 `data/raw/` 下的真实岗位数据（CSV / JSON），仅当 `data/raw/` 无有效数据文件时生成 `data/seed_jobs.json` 演示数据并在页面脚注与头条下标注“含演示数据”；魔搭演示环境注明“演示环境每日重置”。
- **规范化技能表**：独立表 `job_skills(job_id, skill)` + 同义词归一（`app/db/skill_normalizer.py`）。
- **三层 SQL 只读防线**：① SQL 预检（单条 `SELECT` / `WITH...SELECT` + 自动补 `LIMIT 50`）；② `sqlite3.connect("file:data/intel.db?mode=ro", uri=True)` + `conn.set_authorizer()` 仅放行 `SQLITE_SELECT`/`SQLITE_READ`/`SQLITE_FUNCTION` 且禁读 `query_logs` 与非法系统对象；③ `conn.set_progress_handler()` 2 秒查询超时。
- **SSE 流式轨迹**：`POST /api/chat/stream` 使用 `LangGraph astream(stream_mode="updates")`，按节点逐条推送事件。
- **前端结构与审美**：手写 CSS（不使用 Tailwind），多文件拆分（`web/css/` + `web/js/`），`echarts.min.js` / `marked.min.js` 本地托管于 `web/vendor/`；严禁蓝紫渐变、毛玻璃、大圆角阴影卡片、emoji 图标与彩虹色图表。

---

## 三、产品视角、分端 UX 与验收闭环约束（全局规范）

前端（Step 4 ~ Step 6）在落实「情报简报」编辑部风格的同时，必须一次性满足以下跨端硬性底线：

1. **角色 × 设备分层**：
   - **桌面端（`> 980px` / `≥ 1024px`，面向评审/面试官/开发者）**：12 栏网格，「头版 8 栏 + 深色问询台 4 栏」，完整展示 LangGraph 节点执行轨迹、只读 SQL 块与数据出处脚注。
   - **移动端（`≤ 980px` / `< 1024px`，面向真实终端用户/微信随手打开体验者）**：
     - 默认进入**纯净用户态**：主屏 100% 留给核心简报内容，隐藏不必要的内部技术栈标签/架构徽章；
     - 问询台默认收起为**按需展开的底部抽屉**（仅保留轻量入口按钮），抽屉内的技术轨迹与 SQL 细节默认简洁呈现或可折叠展开；
     - 文案按端区分，移动端去掉仅在桌面端成立的方位词（如“右侧面板”）。
2. **移动端布局硬性底线**：
   - 视口高度使用 `height: 100vh; height: 100dvh;`，底部固定操作区增加 `env(safe-area-inset-bottom)` 适配（兼容手机地址栏、微信内置浏览器与魔搭创空间 iframe 外壳）。
   - 严禁固定比例上下分栏强塞两个大面板挤压核心区。
   - 顶部栏 / 报头导航禁止写死固定高度；标题与筛选 Tab 配置 `min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;`，窄屏清爽折行零重叠。
   - 快捷问题 chips 在移动端改为**单行横向滑动胶囊**或提供收起/展开开关（发起交互后可自动收起）。
   - 移动端输入框 `placeholder` 单行不折行；点击发送后自动 `blur()` 收起软键盘，响应结束后**禁止**在移动端强行 `focus()` 弹出键盘遮挡回复。
3. **多视口验收闭环（Step 4 & Step 6 必测）**：
   - 桌面视口：`1440×900` 与 `1280×800`；
   - 平板 / 临界断点：`1024×768` 与 `768×1024`；
   - 主流手机：`390×844`；
   - 矮屏 / 套壳手机：`375×667`（模拟微信/创空间 iframe 压缩高度）；
   - 全部视口均须零横向滚动、零文字重叠、零控制台报错、零 4xx/5xx 资源失败。
4. **人类体验反馈原则**：遇到非技术模糊反馈时先还原现场并截图定位，区分“感受”与“归因”，优先通过响应式分层/折叠交互同时兼顾桌面评审价值与移动端纯净体验。

---

## 四、Git 分支隔离与安全红线

1. **分支隔离**：
   - `main`：本地日常开发与快照；
   - `release/github`：洁净开源发布分支（剔除本地临时文件与隐私配置）；
   - `deploy/modelscope`：魔搭创空间专属部署分支（保留平台专属元数据/配置）。
   - 同步业务代码使用 `git checkout main -- <具体文件路径>`，**严禁将 `main` 强制推送覆盖部署分支**。
2. **密钥与操作安全**：
   - 提交/推送前必查 `git ls-files .env` 输出为空；
   - 临时 Token 推送必须附带 `-c credential.helper=`，严禁将明文 Token 写入 `.git/config` 或任何文件；
   - 禁止裸跑 `git reset --hard` 或 `git clean -fd` 销毁未提交改动；回退前必须先备份（如 `git stash` / 备份分支）；
   - Windows 网络/TLS 排障：国内平台走规则模式直连，海外平台显式传 `-c http.proxy=...`；若因中文用户名目录找不到 CA 证书，使用 `-c http.sslCAInfo=<纯英文路径 certifi/cacert.pem>`，切勿关闭 SSL 校验。

---

## 五、分阶段进度表（每完成一步立即更新）

| 阶段 | 内容 | 状态 | 关键产物 | 验收标准 |
| :--- | :--- | :--- | :--- | :--- |
| **Step 0** | 骨架初始化、配置模块与连通性脚本 | **已完成并验收通过** | `AGENTS.md`, `docs/design/*`, `.gitignore`, `.env.example`, `requirements.txt`, `app/config.py`, `scripts/test_llm.py` 及全套占位骨架 | `python scripts/test_llm.py` 打印模型回复、耗时与 token 用量（已实测通过） |
| **Step 1** | 数据层（真实数据优先 + 规范化技能表 + JD 抽取） | **已完成并验收通过** | `app/db/*`, `app/pipeline/extractor.py`, `scripts/test_step1.py` | 入库条数准确（232 条）、3 段 JD 薪资换算准确、`job_skills` 无大小写重复（已实测通过） |
| **Step 2** | LangGraph Text-to-SQL Agent（三层防线 + 自愈 + SSE） | **已完成并验收通过** | `app/agent/*`, `scripts/test_step2.py` | 6 个用例通过；越权与注入均拦截；执行层单测证明只读与授权器兜底；SSE 与自愈闭环实测通过 |
| **Step 3** | FastAPI 服务（SSE + 头条 + 简报推送 + 限流预算） | **已完成并验收通过** | `app/services/*`, `app/main.py`, `scripts/test_api.py` | `/docs` 完整，SSE 逐条输出 `node` 与 `final` 事件（已实测通过） |
| **Step 4** | 设计系统 + 静态高保真稿（Mock 数据，先定长相） | **已完成并验收通过** | `web/css/*`, `web/js/*`, `web/index.html`, `web/mock/*`, `web/vendor/*`, `scripts/gen_mock.py`, `docs/design/screenshots/step4_*.png` | 1440px & 390px 截图符合编辑部风格，强调色 ≤4 处，数字对齐（已实测） |
| **Step 5** | 前端接线（真实接口 + 流式轨迹 + 交互细节） | **已完成并验收通过** | `web/js/api.js`, `web/js/dashboard.js`, `web/js/desk.js`, `web/js/modals.js`, `web/js/main.js`, `web/index.html`, `web/css/components.css` | 问询台节点逐个点亮，零控制台报错，全链路交互跑通（已实测通过） |
| **Step 6** | 视觉 QA 与多视口打磨 | **已完成并验收通过** | `docs/design/screenshots/step6_*.png`, `web/index.html`, `web/css/*`, `web/js/*` | 1440/1280/1024/768/390/375 全视口通过，五态与夜读模式截图完成，对比度与无障碍达标（已实测通过） |
| **Step 7** | Text-to-SQL 自动化消融评测（A/B/C） | **已完成并验收通过** | `data/eval_sql_dataset.json`, `eval/run_eval.py`, `eval/report_generator.py`, `eval/EVAL_REPORT.md`, `eval/report.html` | 30 题全量实跑，报告数据全部来自真实结果（已实测通过） |
| **Step 8** | Docker、README 与魔搭创空间上线 | **已完成并验收通过** | `Dockerfile`, `.dockerignore`, `README.md` | 魔搭创空间 Docker 构建上线、密钥零泄漏、双端访问正常（已实测通过） |

---

## 六、常用验证命令与安全回退方式

> **命令输出约定（用户明确要求）**：后续任何会话向用户提供终端命令时，必须**明确说明所需目录**，并在每段 PowerShell 命令块首行显式带上 `Set-Location d:\Antigravity\industry-intel-agent`（避免默认停在用户主目录报错），且优先直接调用 `.\.venv\Scripts\python.exe` 绕过 PowerShell 脚本执行策略限制。

### 环境初始化与 Step 0 连通性验证（Windows PowerShell）
```powershell
# 0. 进入项目根目录
Set-Location d:\Antigravity\industry-intel-agent

# 1. 创建虚拟环境（若已安装 Python 3.11 建议用 py -3.11 -m venv .venv）
python -m venv .venv

# 2. 使用虚拟环境内的 python 直接安装依赖（绕开 PowerShell Activate.ps1 执行策略限制）
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3. 复制环境变量模板，并手动编辑 .env 填入真实 SILICONFLOW_API_KEY
Copy-Item .env.example .env

# 4. 运行 Step 0 连通性测试
.\.venv\Scripts\python.exe scripts/test_llm.py
```

### 安全检查与回退参考
```powershell
# 密钥防泄漏检查（输出必须为空）
git ls-files .env

# 安全暂存未提交改动（禁止直接 git reset --hard / git clean -fd）
git stash push -u -m "backup-before-rollback"
```
