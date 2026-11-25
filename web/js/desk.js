/**
 * 问询台交互逻辑：真实 SSE 节点轨迹逐行点亮、停止中断、SQL 复制、结果预览表、迷你图与看板联动高亮。
 */

import { ApiError, chatStream } from "/static/js/api.js";
import { getCurrentDashboardSnapshot, highlightDashboardTarget } from "/static/js/dashboard.js";

const miniCharts = [];
const chatHistory = [];
let activeAbort = null;
let hasAskedBefore = false;

const TERMINAL_NODES = new Set([
  "chitchat_answer",
  "security_fallback",
  "error_fallback",
  "insight_synthesizer"
]);

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
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
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

function setSendButtonStreaming(isStreaming) {
  const sendBtn = document.getElementById("desk-send-btn");
  if (!sendBtn) return;
  if (isStreaming) {
    sendBtn.type = "button";
    sendBtn.textContent = "停止";
    sendBtn.classList.add("is-stop");
    sendBtn.disabled = false;
  } else {
    sendBtn.type = "submit";
    sendBtn.textContent = "发送";
    sendBtn.classList.remove("is-stop");
    sendBtn.disabled = false;
  }
}

function renderTraceRowRunning(nodeName) {
  const row = document.createElement("div");
  row.className = "trace-row is-running-row";
  row.dataset.node = nodeName;
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
  row.classList.remove("is-running-row");
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

function predictNextNode(evData) {
  const node = evData.node || "";
  const status = evData.status || "ok";
  const detail = evData.detail || "";

  if (TERMINAL_NODES.has(node)) {
    return null;
  }
  if (node === "intent_router") {
    if (status === "blocked") return "security_fallback";
    if (detail.includes("闲聊") || detail.includes("系统咨询")) return "chitchat_answer";
    return "sql_generator";
  }
  if (node === "sql_generator") {
    return "sql_executor";
  }
  if (node === "sql_executor") {
    if (status === "blocked") return "security_fallback";
    if (status === "retry") return "sql_generator";
    if (status === "error") return "error_fallback";
    return "insight_synthesizer";
  }
  return null;
}

async function streamAnswerText(container, markdownText, signal) {
  const raw = String(markdownText || "");
  if (!raw) return;

  if (prefersReducedMotion()) {
    container.innerHTML = window.marked ? window.marked.parse(raw) : escapeHtml(raw);
    return;
  }

  const plain = raw.replace(/\*\*/g, "");
  let idx = 0;
  const step = plain.length > 160 ? 4 : 2;
  while (idx < plain.length) {
    if (signal && signal.aborted) {
      throw new DOMException("Aborted", "AbortError");
    }
    idx = Math.min(idx + step, plain.length);
    container.textContent = plain.slice(0, idx);
    scrollDeskToBottom();
    await new Promise((r) => setTimeout(r, 16));
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

function renderResultTable(turnEl, columns, rows) {
  if (!Array.isArray(columns) || !columns.length || !Array.isArray(rows) || !rows.length) {
    return;
  }

  const wrap = document.createElement("div");
  wrap.className = "desk-result-wrap";

  const scroll = document.createElement("div");
  scroll.className = "desk-result-scroll";

  const table = document.createElement("table");
  table.className = "mono desk-result-table";

  const thead = document.createElement("thead");
  const headTr = document.createElement("tr");
  columns.forEach((col) => {
    const th = document.createElement("th");
    th.textContent = String(col);
    headTr.appendChild(th);
  });
  thead.appendChild(headTr);
  table.appendChild(thead);

  // 按列检测小数位，确保同一列数字的小数位数一致
  const colHasFloat = new Map();
  columns.forEach((col) => {
    const hasFloat = rows.some((r) => typeof r[col] === "number" && !Number.isInteger(r[col]));
    colHasFloat.set(col, hasFloat);
  });

  const tbody = document.createElement("tbody");
  const buildRows = (limit) => {
    const slice = limit ? rows.slice(0, limit) : rows;
    tbody.innerHTML = slice
      .map((row) => {
        const tds = columns
          .map((col) => {
            const val = row[col];
            const isNum = typeof val === "number";
            const text = val == null
              ? "—"
              : isNum
                ? (colHasFloat.get(col) ? val.toFixed(1) : String(val))
                : String(val);
            return `<td class="${isNum ? "num" : ""}" title="${escapeHtml(text)}">${escapeHtml(text)}</td>`;
          })
          .join("");
        return `<tr>${tds}</tr>`;
      })
      .join("");
  };

  let expanded = false;
  buildRows(8);
  table.appendChild(tbody);
  scroll.appendChild(table);
  wrap.appendChild(scroll);

  if (rows.length > 8) {
    const toggleBtn = document.createElement("button");
    toggleBtn.type = "button";
    toggleBtn.className = "desk-table-toggle";
    toggleBtn.textContent = `展开全部（共 ${rows.length} 行）`;
    toggleBtn.addEventListener("click", () => {
      expanded = !expanded;
      buildRows(expanded ? null : 8);
      toggleBtn.textContent = expanded ? "收起结果表" : `展开全部（共 ${rows.length} 行）`;
      scrollDeskToBottom();
    });
    wrap.appendChild(toggleBtn);
  }

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
  miniCharts.push({ el: box, inst, chart });
}

function detectDashboardTarget(question, sql, answer) {
  const snap = getCurrentDashboardSnapshot();
  if (!snap) return null;

  const corpus = `${question || ""} ${sql || ""} ${answer || ""}`;
  const corpusLower = corpus.toLowerCase();

  let matchedCity = null;
  for (const c of snap.city_compare || []) {
    if (c.city && corpus.includes(c.city)) {
      matchedCity = c.city;
      break;
    }
  }

  let matchedSkill = null;
  for (const s of (snap.skills_top || []).slice(0, 10)) {
    if (s.skill && corpusLower.includes(s.skill.toLowerCase())) {
      matchedSkill = s.skill;
      break;
    }
  }

  if (!matchedCity && !matchedSkill) return null;
  return { city: matchedCity, skill: matchedSkill };
}

function renderTurnActions(turnEl, { sql, target }) {
  const cleanSql = String(sql || "").trim();
  if (!cleanSql && !target) return;

  const bar = document.createElement("div");
  bar.className = "desk-turn-actions";

  if (cleanSql) {
    const copyBtn = document.createElement("button");
    copyBtn.type = "button";
    copyBtn.className = "desk-action-btn";
    copyBtn.textContent = "复制 SQL";
    copyBtn.addEventListener("click", async () => {
      await copyText(cleanSql);
      copyBtn.textContent = "已复制";
      setTimeout(() => {
        copyBtn.textContent = "复制 SQL";
      }, 1500);
    });
    bar.appendChild(copyBtn);
  }

  if (target) {
    const hlBtn = document.createElement("button");
    hlBtn.type = "button";
    hlBtn.className = "desk-action-btn";
    const label = target.skill ? `在看板中高亮 ${target.skill}` : `在看板中高亮 ${target.city}`;
    hlBtn.textContent = label;
    hlBtn.addEventListener("click", () => {
      if (isMobileViewport()) {
        closeDrawer();
      }
      highlightDashboardTarget(target);
    });
    bar.appendChild(hlBtn);
  }

  turnEl.appendChild(bar);
}

export function resizeMiniCharts() {
  miniCharts.forEach((item) => {
    if (item && item.inst) item.inst.resize();
  });
}

export function rerenderMiniChartsForTheme() {
  if (!window.echarts || !window.BriefCharts) return;
  miniCharts.forEach((item) => {
    if (!item || !item.el) return;
    if (item.inst) item.inst.dispose();
    item.inst = window.echarts.init(item.el, "brief-desk");
    item.inst.setOption(window.BriefCharts.miniOption(item.chart), true);
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
  const controller = new AbortController();
  activeAbort = controller;

  const input = document.getElementById("desk-input");
  const messagesEl = document.getElementById("desk-messages");
  const emptyEl = document.getElementById("desk-empty");

  if (emptyEl) emptyEl.classList.add("hidden");
  setSendButtonStreaming(true);

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

  const traceList = turnEl.querySelector(".trace-list");
  // 立即点亮第一个节点占位
  traceList.appendChild(renderTraceRowRunning("intent_router"));
  scrollDeskToBottom();

  let retryCount = 0;
  const historyToSend = chatHistory.slice(-6);

  try {
    await chatStream(
      q,
      historyToSend,
      async (eventName, data) => {
        if (eventName === "node") {
          let row = traceList.querySelector(".trace-row.is-running-row");
          if (!row) {
            row = renderTraceRowRunning(data.node || "agent");
            traceList.appendChild(row);
          }
          if (data.status === "retry") {
            retryCount += 1;
          }
          updateTraceRowDone(row, data, retryCount);

          const nextNode = predictNextNode(data);
          if (nextNode) {
            traceList.appendChild(renderTraceRowRunning(nextNode));
          }
          scrollDeskToBottom();
        } else if (eventName === "error") {
          traceList.querySelectorAll(".is-running-row").forEach((el) => el.remove());
          const errBox = document.createElement("div");
          errBox.className = "desk-error-box";
          errBox.innerHTML = `<div class="desk-box-head mono">问询异常</div><div>${escapeHtml(data?.message || "流式分析异常，请稍后重试。")}</div>`;
          turnEl.appendChild(errBox);
          scrollDeskToBottom();
        } else if (eventName === "final") {
          traceList.querySelectorAll(".is-running-row").forEach((el) => el.remove());

          if (data.sql) {
            renderSqlBlock(turnEl, data.sql);
          }

          if (data.blocked) {
            const box = document.createElement("div");
            box.className = "desk-blocked-box";
            box.innerHTML = `<div class="desk-box-head mono">只读防线 · 已拦截</div><div>${escapeHtml(data.block_reason || data.answer || "该请求已被只读安全防线拦截。")}</div>`;
            turnEl.appendChild(box);
          } else {
            const ansWrap = document.createElement("div");
            ansWrap.innerHTML = '<div class="desk-label">答</div><div class="desk-answer"></div>';
            turnEl.appendChild(ansWrap);
            const ansBody = ansWrap.querySelector(".desk-answer");
            await streamAnswerText(ansBody, data.answer || "", controller.signal);

            renderMiniChart(turnEl, data.chart);
            renderResultTable(turnEl, data.columns, data.rows);

            const target = detectDashboardTarget(q, data.sql, data.answer);
            renderTurnActions(turnEl, { sql: data.sql, target });

            if (data.answer) {
              chatHistory.push({ role: "user", content: q });
              chatHistory.push({ role: "assistant", content: String(data.answer).slice(0, 400) });
              while (chatHistory.length > 6) {
                chatHistory.shift();
              }
            }
          }
          scrollDeskToBottom();
        }
      },
      controller.signal
    );
  } catch (err) {
    traceList.querySelectorAll(".is-running-row").forEach((el) => el.remove());
    if (err && err.name === "AbortError") {
      const stopNote = document.createElement("div");
      stopNote.className = "trace-detail";
      stopNote.textContent = "已手动停止本次问询。";
      turnEl.appendChild(stopNote);
    } else {
      const errBox = document.createElement("div");
      const isRateLimit = err instanceof ApiError && err.status === 429;
      errBox.className = isRateLimit ? "desk-blocked-box" : "desk-error-box";
      const title = isRateLimit ? "限流保护 · 暂缓问询" : "问询异常";
      const msg = err instanceof ApiError ? err.message : `问询异常：${err?.message || err}`;
      errBox.innerHTML = `<div class="desk-box-head mono">${title}</div><div>${escapeHtml(msg)}</div>`;
      turnEl.appendChild(errBox);
    }
  } finally {
    if (activeAbort === controller) {
      activeAbort = null;
      setSendButtonStreaming(false);
    }
  }
}

export function initDesk() {
  const form = document.getElementById("desk-form");
  const input = document.getElementById("desk-input");
  const sendBtn = document.getElementById("desk-send-btn");
  const chipsWrap = document.getElementById("desk-chips");
  const toggleChipsBtn = document.getElementById("btn-toggle-chips");
  const openBtn = document.getElementById("btn-open-desk");
  const closeBtn = document.getElementById("btn-close-desk");
  const backdrop = document.getElementById("desk-backdrop");

  const resetInputHeight = () => {
    if (!input) return;
    input.style.height = "24px";
  };

  if (sendBtn) {
    sendBtn.addEventListener("click", () => {
      if (activeAbort && sendBtn.classList.contains("is-stop")) {
        activeAbort.abort();
      }
    });
  }

  if (form && input) {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      if (activeAbort) return;
      const val = input.value.trim();
      if (!val) return;
      input.value = "";
      resetInputHeight();
      askQuestion(val);
    });

    input.addEventListener("input", () => {
      if (!isMobileViewport()) {
        input.style.height = "24px";
        input.style.height = `${Math.min(input.scrollHeight, 88)}px`;
      }
    });

    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        if (!activeAbort) {
          form.requestSubmit();
        }
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
