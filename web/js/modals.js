/**
 * 模态框管理：投稿一份 JD、生成今日简报、关于本刊。
 */

import { briefMock, extractMock } from "/static/js/api.js";

const SAMPLE_JDS = {
  sz_agent:
    "【深圳南山区 · AI Agent 应用开发工程师 · 25-40K】\n负责企业级多智能体工作流与 RAG 知识库系统开发。要求熟悉 Python、LangGraph、FastAPI、Function Calling 与向量检索调优，有 1-3 年以上大模型落地经验，本科及以上学历。",
  gz_fresh:
    "【广州天河区 · 大模型应用开发工程师（应届/经验不限） · 18-28K】\n参与行业垂直问答助手与 Prompt 工程优化。欢迎 2026 届应届毕业生及经验不限开发者投递，要求扎实的 Python 基础，了解 LLM、RAG 或 Dify 平台。"
};

let lastTriggerEl = null;

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

export function closeModal() {
  const backdrop = document.getElementById("modal-backdrop");
  if (!backdrop || backdrop.classList.contains("hidden")) return;
  backdrop.classList.add("hidden");
  if (lastTriggerEl && typeof lastTriggerEl.focus === "function") {
    lastTriggerEl.focus();
  }
  lastTriggerEl = null;
}

function openModal(title, bodyHtml, triggerEl) {
  lastTriggerEl = triggerEl || document.activeElement;
  const backdrop = document.getElementById("modal-backdrop");
  const titleEl = document.getElementById("modal-title");
  const bodyEl = document.getElementById("modal-body");
  const closeBtn = document.getElementById("btn-close-modal");

  if (!backdrop || !titleEl || !bodyEl) return;
  titleEl.textContent = title;
  bodyEl.innerHTML = bodyHtml;
  backdrop.classList.remove("hidden");
  if (closeBtn) closeBtn.focus();
}

function renderExtractReport(res) {
  const job = res.job || {};
  const fields = [
    ["岗位名称", job.title || "—"],
    ["招聘公司", job.company || "—"],
    ["所在城市", `${job.city || "—"} ${job.district || ""}`.trim()],
    ["月薪范围", `${job.salary_min ?? "—"}–${job.salary_max ?? "—"}K（均值 ${job.salary_avg ?? "—"}K）`],
    ["经验要求", job.experience || "—"],
    ["学历要求", job.education || "—"],
    ["岗位类别", job.category || "—"],
    ["应届友好", job.fresh_grad_friendly ? "是" : "否"],
    ["规范化技能", (job.skills || []).join(" · ") || "—"]
  ];

  return fields
    .map(
      ([k, v]) =>
        `<div class="report-row"><dt>${escapeHtml(k)}</dt><dd class="mono">${escapeHtml(v)}</dd></div>`
    )
    .join("");
}

export function openExtractModal(triggerEl) {
  const html = `
    <div class="extract-grid">
      <div>
        <label for="jd-raw-input" class="kicker" style="display:block;margin-bottom:8px">粘贴原始 JD 文本</label>
        <textarea id="jd-raw-input" class="jd-textarea" placeholder="在此粘贴岗位描述文本，系统将结构化抽取薪资、城市、经验与规范化技能…"></textarea>
        <div class="sample-actions">
          <div class="sample-links">
            <button type="button" class="link" data-sample="sz_agent">示例一：深圳 Agent 岗</button>
            <button type="button" class="link" data-sample="gz_fresh">示例二：广州应届岗</button>
          </div>
          <button type="button" class="btn-outline" id="btn-run-extract">抽取并预览</button>
        </div>
        <div class="status-msg" id="extract-status">提示：当前为静态高保真预览，抽取结果来自本地模拟。</div>
      </div>
      <div>
        <div class="kicker" style="margin-bottom:8px">结构化抽取报表</div>
        <dl class="report-dl" id="extract-report">
          <div class="report-row"><dt>状态</dt><dd class="mono">等待输入 JD 文本</dd></div>
        </dl>
      </div>
    </div>`;

  openModal("投稿一份 JD", html, triggerEl);

  const textarea = document.getElementById("jd-raw-input");
  const reportEl = document.getElementById("extract-report");
  const statusEl = document.getElementById("extract-status");
  const runBtn = document.getElementById("btn-run-extract");

  document.querySelectorAll("[data-sample]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (textarea) textarea.value = SAMPLE_JDS[btn.dataset.sample] || "";
    });
  });

  if (runBtn && textarea && reportEl && statusEl) {
    runBtn.addEventListener("click", async () => {
      const txt = textarea.value.trim();
      if (txt.length < 5) {
        statusEl.textContent = "请先输入或填充一段不少于 5 个字的 JD 文本。";
        return;
      }
      runBtn.disabled = true;
      statusEl.textContent = "正在抽取结构化字段…";
      reportEl.innerHTML = '<div class="skeleton-bar" style="width:100%"></div><div class="skeleton-bar" style="width:78%"></div>';
      try {
        const res = await extractMock(txt);
        reportEl.innerHTML = renderExtractReport(res);
        statusEl.textContent = "抽取完成（静态稿模拟结果，Step 5 接入真实入库）。";
      } finally {
        runBtn.disabled = false;
      }
    });
  }
}

export async function openBriefModal(currentCity, triggerEl) {
  openModal("生成今日简报", '<div class="skeleton-bar" style="width:65%"></div>', triggerEl);
  const bodyEl = document.getElementById("modal-body");
  if (!bodyEl) return;

  const res = await briefMock(currentCity);
  const rendered = window.marked ? window.marked.parse(res.markdown || "") : `<pre>${escapeHtml(res.markdown)}</pre>`;

  bodyEl.innerHTML = `
    <div class="brief-paper">${rendered}</div>
    <div class="webhook-row">
      <label for="brief-webhook-input" class="sr-only">Webhook 地址</label>
      <input id="brief-webhook-input" class="webhook-input" type="url" placeholder="可选：输入飞书 / 钉钉 / 企微 Webhook 地址">
      <button type="button" class="btn-outline" id="btn-push-brief">推送简报</button>
    </div>
    <div class="status-msg" id="brief-status">${escapeHtml(res.message || "")}</div>`;

  const pushBtn = document.getElementById("btn-push-brief");
  const statusEl = document.getElementById("brief-status");
  if (pushBtn && statusEl) {
    pushBtn.addEventListener("click", () => {
      statusEl.textContent = "静态稿：预览模式，Step 5 将接入真实 Webhook 推送。";
    });
  }
}

export function openAboutModal(triggerEl) {
  const html = `
    <div style="display:flex;flex-direction:column;gap:12px;font-size:14px;line-height:1.75">
      <p><strong>INDUSTRY INTEL</strong> 是一个垂直行业岗位情报采集与对话式分析 Agent，聚焦粤港澳大湾区（广深莞）AI 与大模型工程岗位。</p>
      <p>技术架构遵循三项工程原则：真实岗位数据优先入库并建立第一范式规范化技能表（<code>job_skills</code>）；自然语言问答通过 LangGraph 多节点图执行，底层以三层只读防线（SQL 预检、<code>?mode=ro</code> + <code>set_authorizer</code>、2 秒查询超时）保障安全；前端采用「情报简报」编辑部版式，强调结论先行与数据出处。</p>
      <p style="color:var(--ink-3);font-size:12px">注：当前页面处于 Step 4 静态高保真稿阶段，使用离线模拟数据展示版式与交互流程。</p>
    </div>`;
  openModal("关于本刊", html, triggerEl);
}

export function initModals(getCityFn) {
  const backdrop = document.getElementById("modal-backdrop");
  const closeBtn = document.getElementById("btn-close-modal");
  const extractBtn = document.getElementById("btn-open-extract");
  const briefBtn = document.getElementById("btn-open-brief");
  const aboutBtn = document.getElementById("btn-open-about");

  if (closeBtn) closeBtn.addEventListener("click", closeModal);
  if (backdrop) {
    backdrop.addEventListener("click", (e) => {
      if (e.target === backdrop) closeModal();
    });
  }
  if (extractBtn) extractBtn.addEventListener("click", () => openExtractModal(extractBtn));
  if (briefBtn) briefBtn.addEventListener("click", () => openBriefModal(getCityFn(), briefBtn));
  if (aboutBtn) aboutBtn.addEventListener("click", () => openAboutModal(aboutBtn));
}
