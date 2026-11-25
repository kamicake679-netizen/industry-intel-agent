/**
 * 模态框管理：投稿一份 JD（真实抽取入库）、生成今日简报（真实生成与 Webhook 推送）、关于本刊。
 */

import { ApiError, extractJob, generateBrief } from "/static/js/api.js";

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
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
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
  const salaryRange =
    job.salary_min != null && job.salary_max != null
      ? `${job.salary_min}–${job.salary_max}K（均值 ${job.salary_avg ?? "—"}K）`
      : `均值 ${job.salary_avg ?? "—"}K`;
  const fields = [
    ["入库状态", res.is_new ? "新增入库（已同步更新看板）" : "库中已存在相同岗位（去重返回）"],
    ["岗位名称", job.title || "—"],
    ["招聘公司", job.company || "—"],
    ["所在城市", `${job.city || "—"} ${job.district || ""}`.trim()],
    ["月薪范围", salaryRange],
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

export function openExtractModal(triggerEl, { onJobSubmitted } = {}) {
  const html = `
    <div class="extract-grid">
      <div>
        <label for="jd-raw-input" class="kicker" style="display:block;margin-bottom:8px">粘贴原始 JD 文本</label>
        <textarea id="jd-raw-input" class="jd-textarea" placeholder="在此粘贴岗位描述文本，系统将调用 LLM 结构化抽取薪资、城市、经验与规范化技能并入库…"></textarea>
        <div class="sample-actions">
          <div class="sample-links">
            <button type="button" class="link" data-sample="sz_agent">示例一：深圳 Agent 岗</button>
            <button type="button" class="link" data-sample="gz_fresh">示例二：广州应届岗</button>
          </div>
          <button type="button" class="btn-outline" id="btn-run-extract">抽取并入库</button>
        </div>
        <div class="status-msg" id="extract-status">提交后将调用 LLM 抽取结构化字段，并同步写入规范化技能表。</div>
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
      statusEl.textContent = "正在调用模型抽取结构化字段并写入数据库…";
      reportEl.innerHTML = '<div class="skeleton-bar" style="width:100%"></div><div class="skeleton-bar" style="width:78%"></div>';
      try {
        const res = await extractJob(txt);
        reportEl.innerHTML = renderExtractReport(res);
        statusEl.textContent = res.is_new
          ? "已结构化抽取并入库，头版 KPI 与最新入库时间线已同步更新。"
          : "抽取完成：库内已存在相同岗位哈希，已返回既有结构化记录。";
        if (onJobSubmitted) {
          await onJobSubmitted(res);
        }
      } catch (err) {
        const msg = err instanceof ApiError ? err.message : `抽取失败：${err?.message || err}`;
        statusEl.textContent = msg;
        reportEl.innerHTML = `<div class="report-row"><dt>异常</dt><dd class="mono">${escapeHtml(msg)}</dd></div>`;
      } finally {
        runBtn.disabled = false;
      }
    });
  }
}

export async function openBriefModal(currentCity, triggerEl) {
  openModal(
    "生成今日简报",
    '<div class="skeleton-bar" style="width:65%;margin-bottom:8px"></div><div class="skeleton-bar" style="width:92%"></div>',
    triggerEl
  );
  const bodyEl = document.getElementById("modal-body");
  if (!bodyEl) return;

  try {
    const res = await generateBrief({ city: currentCity, push: false });
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
    const webhookInput = document.getElementById("brief-webhook-input");
    const statusEl = document.getElementById("brief-status");

    if (pushBtn && statusEl) {
      pushBtn.addEventListener("click", async () => {
        pushBtn.disabled = true;
        statusEl.textContent = "正在生成并推送简报…";
        try {
          const pushRes = await generateBrief({
            city: currentCity,
            webhookUrl: webhookInput ? webhookInput.value : "",
            push: true
          });
          statusEl.textContent = pushRes.message || (pushRes.sent ? "Webhook 推送成功。" : "已返回预览模式。");
        } catch (err) {
          statusEl.textContent = err instanceof ApiError ? err.message : `推送失败：${err?.message || err}`;
        } finally {
          pushBtn.disabled = false;
        }
      });
    }
  } catch (err) {
    const msg = err instanceof ApiError ? err.message : `简报生成失败：${err?.message || err}`;
    bodyEl.innerHTML = `<div class="status-msg">${escapeHtml(msg)}</div>`;
  }
}

export function openAboutModal(triggerEl) {
  const html = `
    <div style="display:flex;flex-direction:column;gap:12px;font-size:14px;line-height:1.75">
      <p><strong>INDUSTRY INTEL</strong> 是一个垂直行业岗位情报采集与对话式分析 Agent，聚焦粤港澳大湾区（广深莞）AI 与大模型工程岗位。</p>
      <p>技术架构遵循三项工程原则：真实岗位数据优先入库并建立第一范式规范化技能表（<code>job_skills</code>）；自然语言问答通过 LangGraph 多节点图实时流式执行，底层以三层只读防线（SQL 预检、<code>?mode=ro</code> + <code>set_authorizer</code>、2 秒查询超时）保障安全；前端采用「情报简报」编辑部版式，强调结论先行与数据出处。</p>
      <p style="color:var(--ink-3);font-size:12px">说明：当前页面已接入实时后端接口，自然语言问询受单 IP 限流与每日调用预算保护；演示环境容器重启后新增数据会重置。</p>
    </div>`;
  openModal("关于本刊", html, triggerEl);
}

export function initModals(getCityFn, { onJobSubmitted } = {}) {
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
  if (extractBtn) extractBtn.addEventListener("click", () => openExtractModal(extractBtn, { onJobSubmitted }));
  if (briefBtn) briefBtn.addEventListener("click", () => openBriefModal(getCityFn(), briefBtn));
  if (aboutBtn) aboutBtn.addEventListener("click", () => openAboutModal(aboutBtn));
}
