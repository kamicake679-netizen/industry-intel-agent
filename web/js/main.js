/**
 * 前端总入口：初始化主题、加载看板 Mock、绑定交互、支持 QA 与 Demo 钩子。
 */

import { getDashboard, getHeadline } from "/static/js/api.js";
import { renderDashboard, rerenderChartsForTheme } from "/static/js/dashboard.js";
import { askQuestion, closeDrawer, initDesk, openDrawer } from "/static/js/desk.js";
import { closeModal, initModals } from "/static/js/modals.js";

const runtimeErrors = [];
let currentCity = "全部";

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

async function switchCity(city) {
  currentCity = city || "全部";
  document.querySelectorAll("#city-tabs .tab").forEach((tab) => {
    const isActive = tab.dataset.city === currentCity;
    tab.classList.toggle("on", isActive);
    tab.setAttribute("aria-selected", isActive ? "true" : "false");
  });

  const [dash, head] = await Promise.all([getDashboard(currentCity), getHeadline(currentCity)]);
  renderDashboard(dash, head);
  updateQaDataset();
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

async function bootstrap() {
  initThemeToggle();
  if (window.BriefCharts) {
    window.BriefCharts.register();
  }

  document.querySelectorAll("#city-tabs .tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      switchCity(tab.dataset.city || "全部");
    });
  });

  initDesk();
  initModals(() => currentCity);
  initGlobalShortcuts();

  await switchCity("全部");

  const params = new URLSearchParams(window.location.search);
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
