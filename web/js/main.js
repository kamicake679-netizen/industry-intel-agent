/**
 * 前端总入口：初始化主题、注册全局 429 提示、加载真实看板与头条、同步 URL ?city= 并绑定全局交互。
 */

import { ApiError, getDashboard, getHeadline, getHealth, setApiNoticeHandler } from "/static/js/api.js";
import {
  prependSubmittedJob,
  renderDashboard,
  renderHeadline,
  rerenderChartsForTheme,
  setDashboardLoading
} from "/static/js/dashboard.js";
import { askQuestion, closeDrawer, initDesk, openDrawer, rerenderMiniChartsForTheme } from "/static/js/desk.js";
import { closeModal, initModals } from "/static/js/modals.js";

const VALID_CITIES = new Set(["全部", "广州", "深圳", "东莞"]);
const runtimeErrors = [];
let currentCity = "全部";
let switchSeq = 0;

window.addEventListener("error", (e) => {
  runtimeErrors.push(e.message || String(e));
});

window.addEventListener("unhandledrejection", (e) => {
  runtimeErrors.push(String(e.reason || "unhandledrejection"));
});

function updateQaDataset() {
  const params = new URLSearchParams(window.location.search);
  if (params.get("qa") !== "1") return;
  document.documentElement.dataset.qa = JSON.stringify({
    sw: document.documentElement.scrollWidth,
    iw: window.innerWidth,
    errors: runtimeErrors
  });
}

let noticeRetryFn = null;

function showNotice(tag, message, { variant = "ratelimit", onRetry = null } = {}) {
  const banner = document.getElementById("notice-banner");
  const tagEl = document.getElementById("notice-tag");
  const textEl = document.getElementById("notice-text");
  const retryBtn = document.getElementById("btn-retry-notice");
  if (!banner || !textEl) return;
  if (tagEl && tag) tagEl.textContent = tag;
  textEl.textContent = message;
  banner.classList.toggle("is-error", variant === "error");
  noticeRetryFn = typeof onRetry === "function" ? onRetry : null;
  if (retryBtn) {
    retryBtn.classList.toggle("hidden", !noticeRetryFn);
  }
  banner.classList.remove("hidden");
}

function hideNotice() {
  const banner = document.getElementById("notice-banner");
  if (banner) banner.classList.add("hidden");
  noticeRetryFn = null;
}

function syncCityToUrl(city) {
  const url = new URL(window.location.href);
  if (!city || city === "全部") {
    url.searchParams.delete("city");
  } else {
    url.searchParams.set("city", city);
  }
  window.history.replaceState(null, "", url.toString());
}

async function switchCity(city, { syncUrl = true } = {}) {
  const targetCity = VALID_CITIES.has(city) ? city : "全部";
  currentCity = targetCity;
  const seq = ++switchSeq;

  document.querySelectorAll("#city-tabs .tab").forEach((tab) => {
    const isActive = tab.dataset.city === currentCity;
    tab.classList.toggle("on", isActive);
    tab.setAttribute("aria-selected", isActive ? "true" : "false");
  });

  if (syncUrl) {
    syncCityToUrl(currentCity);
  }

  hideNotice();
  setDashboardLoading(true);

  const dashPromise = getDashboard(currentCity)
    .then((dash) => {
      if (seq !== switchSeq) return;
      renderDashboard(dash, null);
      updateQaDataset();
    })
    .catch((err) => {
      if (seq !== switchSeq) return;
      const msg = err instanceof ApiError ? err.message : `看板加载失败：${err?.message || err}`;
      showNotice("编务异常", msg, {
        variant: "error",
        onRetry: () => switchCity(currentCity, { syncUrl: false })
      });
      runtimeErrors.push(msg);
    });

  const headPromise = getHeadline(currentCity)
    .then((head) => {
      if (seq !== switchSeq) return;
      renderHeadline(head);
    })
    .catch((err) => {
      if (seq !== switchSeq) return;
      renderHeadline({
        kicker: "头条 · 本周观察",
        title: `${currentCity === "全部" ? "广深莞" : currentCity} AI 岗位情报已就绪`,
        dek: err instanceof ApiError ? err.message : "头条摘要暂未生成，请先浏览下方核心指标与图表。"
      });
    });

  await Promise.allSettled([dashPromise, headPromise]);
}

function initThemeToggle() {
  const btn = document.getElementById("btn-toggle-theme");
  const saved = localStorage.getItem("intel_theme");
  if (saved === "night") {
    document.documentElement.dataset.theme = "night";
    if (btn) btn.setAttribute("aria-pressed", "true");
    if (btn) btn.textContent = "日读";
  }

  if (!btn) return;
  btn.addEventListener("click", () => {
    const isNight = document.documentElement.dataset.theme === "night";
    if (isNight) {
      delete document.documentElement.dataset.theme;
      localStorage.setItem("intel_theme", "day");
      btn.setAttribute("aria-pressed", "false");
      btn.textContent = "夜读";
    } else {
      document.documentElement.dataset.theme = "night";
      localStorage.setItem("intel_theme", "night");
      btn.setAttribute("aria-pressed", "true");
      btn.textContent = "日读";
    }
    rerenderChartsForTheme();
    rerenderMiniChartsForTheme();
  });
}

function initGlobalShortcuts() {
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeModal();
      closeDrawer();
      return;
    }

    const activeTag = document.activeElement?.tagName?.toLowerCase();
    if (e.key === "/" && activeTag !== "input" && activeTag !== "textarea") {
      const input = document.getElementById("desk-input");
      if (input && window.innerWidth >= 1024) {
        e.preventDefault();
        input.focus();
      }
    }
  });
}

async function syncModelBadge() {
  try {
    const health = await getHealth();
    const badge = document.querySelector(".desk-model-badge");
    if (badge && health?.llm_model) {
      const shortName = String(health.llm_model).split("/").pop() || health.llm_model;
      const dotClass = health.llm_configured ? "is-moss" : "is-retry";
      badge.innerHTML = `<span class="dot ${dotClass}" aria-hidden="true"></span>${shortName}`;
    }
  } catch {
    // 健康检查失败时不阻塞主界面
  }
}

async function bootstrap() {
  initThemeToggle();
  setApiNoticeHandler(({ tag, message }) => showNotice(tag, message));

  const closeNoticeBtn = document.getElementById("btn-close-notice");
  if (closeNoticeBtn) {
    closeNoticeBtn.addEventListener("click", hideNotice);
  }
  const retryNoticeBtn = document.getElementById("btn-retry-notice");
  if (retryNoticeBtn) {
    retryNoticeBtn.addEventListener("click", () => {
      if (noticeRetryFn) noticeRetryFn();
    });
  }

  if (window.BriefCharts) {
    window.BriefCharts.register();
  }

  document.querySelectorAll("#city-tabs .tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      switchCity(tab.dataset.city || "全部");
    });
  });

  initDesk();
  initModals(() => currentCity, {
    onJobSubmitted: async (res) => {
      if (res?.job) {
        prependSubmittedJob(res.job);
      }
      if (res?.is_new) {
        try {
          const dash = await getDashboard(currentCity);
          renderDashboard(dash, null);
          if (res.job) {
            prependSubmittedJob(res.job);
          }
        } catch {
          // 忽略静默刷新失败
        }
      }
    }
  });
  initGlobalShortcuts();

  const params = new URLSearchParams(window.location.search);
  const initialCity = params.get("city") || "全部";
  await Promise.allSettled([switchCity(initialCity, { syncUrl: false }), syncModelBadge()]);

  if (params.get("drawer") === "1") {
    openDrawer();
  }
  if (params.get("demo") === "1") {
    askQuestion("广州不限经验要求 RAG 的岗位均薪");
  }

  if (params.get("qa") === "1") {
    setTimeout(updateQaDataset, 1500);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  bootstrap().catch((err) => {
    runtimeErrors.push(err.message || String(err));
    updateQaDataset();
  });
});
