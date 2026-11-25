/**
 * 问询台交互逻辑：SSE 节点轨迹逐行点亮、SQL 复制、答案流式渲染、迷你图与移动端底部抽屉。
 */

import { chatStreamMock } from "/static/js/api.js";

const miniCharts = [];
let activeAbort = null;
let hasAskedBefore = false;

function isMobileViewport() {
  return Boolean(window.matchMedia && window.matchMedia("(max-width: 1023.98px)").matches);
}

function prefersReducedMotion() {
  return Boolean(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
}

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text);
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try {
    document.execCommand("copy");
  } finally {
    document.body.removeChild(ta);
  }
  return Promise.resolve();
}

function scrollDeskToBottom() {
  const body = document.getElementById("desk-messages");
  if (body) {
    body.scrollTop = body.scrollHeight;
  }
}

function renderTraceRowRunning(nodeName) {
  const row = document.createElement("div");
  row.className = "trace-row";
  row.innerHTML = `
    <span class="dot is-running pulse" aria-hidden="true"></span>
    <div class="trace-content">
      <div class="trace-main">
        <span><span class="mono trace-node">${escapeHtml(nodeName)}</span> <span class="trace-tag-running">运行中…</span></span>
        <span class="mono trace-ms">—</span>
      </div>
      <div class="trace-detail"></div>
    </div>`;
  return row;
}

function updateTraceRowDone(row, evData, retryCount) {
  const dot = row.querySelector(".dot");
  const mainLeft = row.querySelector(".trace-main > span:first-child");
  const msEl = row.querySelector(".trace-ms");
  const detailEl = row.querySelector(".trace-detail");

  const status = evData.status || "ok";
  dot.className = "dot";
  if (status === "retry") {
    dot.classList.add("is-retry");
    mainLeft.innerHTML = `<span class="mono trace-node">${escapeHtml(evData.node)}</span><span class="trace-tag-retry">自愈 ×${retryCount}</span>`;
  } else if (status === "blocked") {
    dot.classList.add("is-blocked");
    mainLeft.innerHTML = `<span class="mono trace-node">${escapeHtml(evData.node)}</span><span class="trace-tag-blocked">已拦截</span>`;
  } else if (status === "error") {
    dot.classList.add("is-error");
    mainLeft.innerHTML = `<span class="mono trace-node">${escapeHtml(evData.node)}</span><span class="trace-tag-retry">异常</span>`;
  } else {
    dot.classList.add("is-ok");
    mainLeft.innerHTML = `<span class="mono trace-node">${escapeHtml(evData.node)}</span>`;
  }

  if (msEl) {
    msEl.textContent = `${Math.round(Number(evData.ms || 0))}ms`;
  }
  if (detailEl && evData.detail) {
    detailEl.textContent = evData.detail;
  }
}

async function streamAnswerText(container, markdownText) {
  const raw = String(markdownText || "");
  if (!raw) return;

  if (prefersReducedMotion()) {
    container.innerHTML = window.marked ? window.marked.parse(raw) : escapeHtml(raw);
    return;
  }

  // 逐字输出纯文本预览，结束后交由 marked 渲染结构化排版
  const plain = raw.replace(/\*\*/g, "");
  let idx = 0;
  while (idx < plain.length) {
    idx = Math.min(idx + 2, plain.length);
    container.textContent = plain.slice(0, idx);
    scrollDeskToBottom();
    await new Promise((r) => setTimeout(r, 18));
  }
  container.innerHTML = window.marked ? window.marked.parse(raw) : escapeHtml(raw);
}

function renderSqlBlock(turnEl, sql) {
  const cleanSql = String(sql || "").trim();
  if (!cleanSql) return;

  const wrap = document.createElement("div");
  wrap.className = "sql-wrap";
  const fullSql = `-- 只读连接 · 已自动限制 LIMIT\n${cleanSql}`;
  wrap.innerHTML = `
    <div class="sql-toolbar">
      <span class="mono">SQL · 只读</span>
      <button type="button" class="sql-copy-btn">复制 SQL</button>
    </div>
    <pre class="mono sql-pre">${escapeHtml(fullSql)}</pre>`;

  const copyBtn = wrap.querySelector(".sql-copy-btn");
  copyBtn.addEventListener("click", async () => {
    await copyText(cleanSql);
    copyBtn.textContent = "已复制";
    setTimeout(() => {
      copyBtn.textContent = "复制 SQL";
    }, 1500);
  });

  turnEl.appendChild(wrap);
}

function renderMiniChart(turnEl, chart) {
  if (!chart || chart.type === "none" || !Array.isArray(chart.labels) || !chart.labels.length) {
    return;
  }
  if (!window.echarts || !window.BriefCharts) return;

  const box = document.createElement("div");
  box.className = "desk-mini-chart";
  box.setAttribute("role", "img");
  box.setAttribute("aria-label", chart.title || "问询结果迷你图");
  turnEl.appendChild(box);

  const inst = window.echarts.init(box, "brief-desk");
  inst.setOption(window.BriefCharts.miniOption(chart), true);
  miniCharts.push(inst);
}

export function resizeMiniCharts() {
  miniCharts.forEach((c) => {
    if (c) c.resize();
  });
}

export function openDrawer() {
  const panel = document.getElementById("desk-panel");
  const backdrop = document.getElementById("desk-backdrop");
  const fab = document.getElementById("btn-open-desk");
  if (panel) panel.classList.add("open");
  if (backdrop) backdrop.classList.add("open");
  if (fab) fab.setAttribute("aria-expanded", "true");
  setTimeout(resizeMiniCharts, 260);
}

export function closeDrawer() {
  const panel = document.getElementById("desk-panel");
  const backdrop = document.getElementById("desk-backdrop");
  const fab = document.getElementById("btn-open-desk");
  if (panel) panel.classList.remove("open");
  if (backdrop) backdrop.classList.remove("open");
  if (fab) fab.setAttribute("aria-expanded", "false");
}

export async function askQuestion(questionText) {
  const q = String(questionText || "").trim();
  if (!q) return;

  if (activeAbort) {
    activeAbort.abort();
  }
  activeAbort = new AbortController();

  const sendBtn = document.getElementById("desk-send-btn");
  const input = document.getElementById("desk-input");
  const messagesEl = document.getElementById("desk-messages");
  const emptyEl = document.getElementById("desk-empty");

  if (emptyEl) emptyEl.classList.add("hidden");
  if (sendBtn) sendBtn.disabled = true;

  // 移动端首次提问后自动收起快捷问题胶囊
  if (!hasAskedBefore && isMobileViewport()) {
    hasAskedBefore = true;
    const chipsWrap = document.getElementById("desk-chips");
    const chipsHeader = document.getElementById("chips-header");
    if (chipsWrap) chipsWrap.classList.add("hidden");
    if (chipsHeader) chipsHeader.classList.add("is-visible");
  } else {
    hasAskedBefore = true;
  }

  if (input && isMobileViewport()) {
    input.blur();
  }

  const turnEl = document.createElement("div");
  turnEl.className = "desk-turn";
  turnEl.innerHTML = `
    <div>
      <div class="desk-label">问</div>
      <div class="desk-q-text">${escapeHtml(q)}</div>
    </div>
    <div class="trace-list"></div>`;
  messagesEl.appendChild(turnEl);
  scrollDeskToBottom();

  const traceList = turnEl.querySelector(".trace-list");
  let retryCount = 0;

  try {
    await chatStreamMock(
      q,
      async (eventName, data) => {
        if (eventName === "node") {
          const row = renderTraceRowRunning(data.node || "agent");
          traceList.appendChild(row);
          scrollDeskToBottom();

          if (data.status === "retry") {
            retryCount += 1;
          }
          await new Promise((r) => setTimeout(r, prefersReducedMotion() ? 0 : 180));
          updateTraceRowDone(row, data, retryCount);
          scrollDeskToBottom();
        } else if (eventName === "final") {
          if (data.sql) {
            renderSqlBlock(turnEl, data.sql);
          }

          if (data.blocked) {
            const box = document.createElement("div");
            box.className = "desk-blocked-box";
            box.textContent = data.block_reason || "该请求已被只读安全防线拦截。";
            turnEl.appendChild(box);
          } else {
            const ansWrap = document.createElement("div");
            ansWrap.innerHTML = '<div class="desk-label">答</div><div class="desk-answer"></div>';
            turnEl.appendChild(ansWrap);
            const ansBody = ansWrap.querySelector(".desk-answer");
            await streamAnswerText(ansBody, data.answer || "");
            renderMiniChart(turnEl, data.chart);
          }
          scrollDeskToBottom();
        }
      },
      activeAbort.signal
    );
  } catch (err) {
    if (err.name !== "AbortError") {
      const errBox = document.createElement("div");
      errBox.className = "desk-blocked-box";
      errBox.textContent = `问询异常：${err.message || err}`;
      turnEl.appendChild(errBox);
    }
  } finally {
    if (sendBtn) sendBtn.disabled = false;
    activeAbort = null;
  }
}

export function initDesk() {
  const form = document.getElementById("desk-form");
  const input = document.getElementById("desk-input");
  const chipsWrap = document.getElementById("desk-chips");
  const toggleChipsBtn = document.getElementById("btn-toggle-chips");
  const openBtn = document.getElementById("btn-open-desk");
  const closeBtn = document.getElementById("btn-close-desk");
  const backdrop = document.getElementById("desk-backdrop");

  if (form && input) {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const val = input.value.trim();
      if (!val) return;
      input.value = "";
      askQuestion(val);
    });

    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        form.requestSubmit();
      }
    });
  }

  if (chipsWrap) {
    chipsWrap.addEventListener("click", (e) => {
      const chip = e.target.closest(".chip");
      if (!chip) return;
      const q = chip.dataset.q || chip.textContent;
      askQuestion(q);
    });
  }

  if (toggleChipsBtn && chipsWrap) {
    toggleChipsBtn.addEventListener("click", () => {
      const hidden = chipsWrap.classList.toggle("hidden");
      toggleChipsBtn.textContent = hidden ? "展开快捷问题" : "收起快捷问题";
    });
  }

  if (openBtn) openBtn.addEventListener("click", openDrawer);
  if (closeBtn) closeBtn.addEventListener("click", closeDrawer);
  if (backdrop) backdrop.addEventListener("click", closeDrawer);
}
