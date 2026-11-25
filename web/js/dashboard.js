/**
 * 头版看板渲染：报头、头条、KPI count-up、ECharts 图表、三城表与最新入库时间线。
 */

const chartInstances = {
  skills: null,
  exp: null,
  category: null
};

let lastDashboardData = null;

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

/**
 * 数字平滑计数动画（600ms，easeOutCubic）。
 */
function animateNumber(el, target, decimals = 0, suffixHtml = "") {
  const endVal = Number(target) || 0;
  if (prefersReducedMotion()) {
    el.innerHTML = `${endVal.toFixed(decimals)}${suffixHtml}`;
    return;
  }

  const startVal = 0;
  const duration = 600;
  const startTime = performance.now();
  let finished = false;

  setTimeout(() => {
    if (!finished) {
      finished = true;
      el.innerHTML = `${endVal.toFixed(decimals)}${suffixHtml}`;
    }
  }, duration + 40);

  function frame(now) {
    if (finished) return;
    const progress = Math.min((now - startTime) / duration, 1);
    const eased = 1 - Math.pow(1 - progress, 3);
    const current = startVal + (endVal - startVal) * eased;
    el.innerHTML = `${current.toFixed(decimals)}${suffixHtml}`;
    if (progress < 1) {
      requestAnimationFrame(frame);
    } else {
      finished = true;
      el.innerHTML = `${endVal.toFixed(decimals)}${suffixHtml}`;
    }
  }

  requestAnimationFrame(frame);
}

function formatIssueMeta(updatedAt) {
  const now = new Date();
  const startOfYear = new Date(now.getFullYear(), 0, 0);
  const dayOfYear = Math.floor((now - startOfYear) / 86400000);
  const issueNo = String(dayOfYear).padStart(3, "0");

  const yyyy = now.getFullYear();
  const mm = String(now.getMonth() + 1).padStart(2, "0");
  const dd = String(now.getDate()).padStart(2, "0");
  const weekNames = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
  const week = weekNames[now.getDay()];

  let timeStr = "08:00";
  if (updatedAt && String(updatedAt).includes(" ")) {
    timeStr = String(updatedAt).split(" ")[1].slice(0, 5);
  }
  return `第 ${issueNo} 期 · ${yyyy}-${mm}-${dd} ${week} · 数据更新 ${timeStr}`;
}

function getOrCreateChart(key, elId) {
  if (!window.echarts) return null;
  const el = document.getElementById(elId);
  if (!el) return null;
  if (!chartInstances[key]) {
    chartInstances[key] = window.echarts.init(el, "brief");
    if (window.ResizeObserver) {
      const ro = new ResizeObserver(() => {
        if (chartInstances[key]) {
          chartInstances[key].resize();
        }
      });
      ro.observe(el);
    }
  }
  return chartInstances[key];
}

function renderCharts(data) {
  if (!window.echarts || !window.BriefCharts) return;

  // 图 1：技能提及率 TOP 10
  const skills = (data.skills_top || []).slice(0, 10);
  const skillsChart = getOrCreateChart("skills", "chart-skills");
  if (skillsChart) {
    skillsChart.setOption(
      window.BriefCharts.barOption({
        labels: skills.map((s) => s.skill),
        values: skills.map((s) => s.rate),
        highlightIndex: 0,
        horizontal: true,
        unit: "%",
        fadeAfter: 5
      }),
      true
    );
    const el = document.getElementById("chart-skills");
    if (el && skills.length) {
      el.setAttribute(
        "aria-label",
        `技能提及率前十横向条形图：榜首为 ${skills[0].skill}（${skills[0].rate}%）`
      );
    }
  }

  // 图 2：经验—薪资阶梯
  const expList = data.exp_salary || [];
  const expChart = getOrCreateChart("exp", "chart-exp");
  if (expChart) {
    expChart.setOption(window.BriefCharts.ladderOption(expList), true);
    const el = document.getElementById("chart-exp");
    if (el && expList.length) {
      el.setAttribute(
        "aria-label",
        `经验与薪资阶梯柱状图：共 ${expList.length} 个经验分段`
      );
    }
  }

  // 图 3：岗位大类构成（横向条形）
  const cats = data.category_mix || [];
  const catChart = getOrCreateChart("category", "chart-category");
  if (catChart) {
    catChart.setOption(
      window.BriefCharts.barOption({
        labels: cats.map((c) => c.category),
        values: cats.map((c) => c.rate),
        highlightIndex: -1,
        horizontal: true,
        unit: "%",
        fadeAfter: -1
      }),
      true
    );
    const el = document.getElementById("chart-category");
    if (el && cats.length) {
      el.setAttribute(
        "aria-label",
        `岗位大类构成横向条形图：首位为 ${cats[0].category}（${cats[0].rate}%）`
      );
    }
  }
}

function renderCityTable(compare) {
  const tbody = document.getElementById("city-compare-body");
  const noteEl = document.getElementById("editor-note");
  if (!tbody || !noteEl) return;

  if (!compare || !compare.length) {
    tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--ink-3)">暂无对比数据</td></tr>';
    noteEl.textContent = "";
    return;
  }

  const maxFresh = Math.max(...compare.map((c) => Number(c.fresh_grad_rate) || 0));
  tbody.innerHTML = compare
    .map((c) => {
      const isTopFresh = Number(c.fresh_grad_rate) === maxFresh;
      return `<tr>
        <td style="font-family:var(--sans)">${escapeHtml(c.city)}</td>
        <td class="num">${Number(c.job_count)}</td>
        <td class="num">${Number(c.avg_salary).toFixed(1)}K</td>
        <td class="num ${isTopFresh ? "is-highlight" : ""}">${Number(c.fresh_grad_rate).toFixed(1)}%</td>
      </tr>`;
    })
    .join("");

  const sortedByFresh = [...compare].sort((a, b) => b.fresh_grad_rate - a.fresh_grad_rate);
  const best = sortedByFresh[0];
  const lowest = sortedByFresh[sortedByFresh.length - 1];
  const ratio = lowest.fresh_grad_rate > 0 ? (best.fresh_grad_rate / lowest.fresh_grad_rate).toFixed(1) : "1.0";

  noteEl.innerHTML = `<span class="editor-note-lead">编者按　</span>${escapeHtml(best.city)}的应届友好岗位占比（${Number(best.fresh_grad_rate).toFixed(1)}%）最高，是${escapeHtml(lowest.city)}（${Number(lowest.fresh_grad_rate).toFixed(1)}%）的 ${ratio} 倍，零经验转行者可优先关注。`;
}

function renderTimeline(latest) {
  const listEl = document.getElementById("latest-timeline");
  const countEl = document.getElementById("timeline-count");
  if (!listEl) return;

  const items = (latest || []).slice(0, 8);
  if (countEl) {
    countEl.textContent = `最近 ${items.length} 条`;
  }

  listEl.innerHTML = items
    .map((item) => {
      const isNew = item.source === "user_submit";
      const minK = item.salary_min != null ? Number(item.salary_min).toFixed(0) : "";
      const maxK = item.salary_max != null ? Number(item.salary_max).toFixed(0) : "";
      const salaryText = minK && maxK ? `${minK}–${maxK}K` : `${Number(item.salary_avg || 0).toFixed(1)}K`;
      const timeText = String(item.created_at || "").slice(5, 16) || "近期";
      const skillsText = (item.skills || []).slice(0, 5).join(" · ") || "—";

      return `<li class="timeline-item ${isNew ? "is-new" : ""}">
        <div class="timeline-top">
          <div class="timeline-title-wrap">
            <span class="timeline-job-title">${escapeHtml(item.title)}</span>
            ${isNew ? '<span class="badge-new">新</span>' : ""}
          </div>
          <span class="mono timeline-salary">${escapeHtml(salaryText)}</span>
        </div>
        <div class="timeline-meta">
          <span class="mono">${escapeHtml(timeText)}</span> · ${escapeHtml(item.company)} · ${escapeHtml(item.city)}${item.district ? " " + escapeHtml(item.district) : ""} · ${escapeHtml(item.experience)}
        </div>
        <div class="timeline-skills">技能：${escapeHtml(skillsText)}</div>
      </li>`;
    })
    .join("");
}

/**
 * 渲染完整头版看板。
 */
export function renderDashboard(data, headline) {
  lastDashboardData = data;
  const kpi = data.kpi || {};
  const meta = data.meta || {};

  const issueEl = document.getElementById("masthead-issue");
  if (issueEl) {
    issueEl.textContent = formatIssueMeta(meta.updated_at);
  }

  // 头条
  const kickerEl = document.getElementById("headline-kicker");
  const titleEl = document.getElementById("headline-title");
  const dekEl = document.getElementById("headline-dek");
  const demoNoteEl = document.getElementById("headline-demo-note");

  if (kickerEl && headline?.kicker) kickerEl.textContent = headline.kicker;
  if (titleEl && headline?.title) titleEl.textContent = headline.title;
  if (dekEl && headline?.dek) dekEl.textContent = headline.dek;
  if (demoNoteEl) {
    demoNoteEl.classList.toggle("hidden", !meta.has_demo_data);
  }

  // KPI 四栏
  const totalEl = document.getElementById("kpi-total");
  const weeklyEl = document.getElementById("kpi-weekly");
  const avgEl = document.getElementById("kpi-avg");
  const medianEl = document.getElementById("kpi-median");
  const freshEl = document.getElementById("kpi-fresh");
  const freshCntEl = document.getElementById("kpi-fresh-count");
  const maxEl = document.getElementById("kpi-max");
  const maxJobEl = document.getElementById("kpi-max-job");

  if (totalEl) animateNumber(totalEl, kpi.total_jobs || 0, 0);
  if (weeklyEl) weeklyEl.textContent = `▲ 本周 +${kpi.weekly_new || 0}`;
  if (avgEl) animateNumber(avgEl, kpi.avg_salary || 0, 1, '<span class="kpi-unit">K</span>');
  if (medianEl) medianEl.textContent = `中位数 ${Number(kpi.median_salary || 0).toFixed(1)}K`;
  if (freshEl) animateNumber(freshEl, kpi.fresh_grad_rate || 0, 1, '<span class="kpi-unit">%</span>');
  if (freshCntEl) freshCntEl.textContent = `${kpi.fresh_grad_count || 0} / ${kpi.total_jobs || 0} 份`;
  if (maxEl) animateNumber(maxEl, kpi.max_salary || 0, 1, '<span class="kpi-unit">K</span>');
  if (maxJobEl && kpi.max_salary_job) {
    maxJobEl.textContent = `${kpi.max_salary_job.city} · ${kpi.max_salary_job.title}`;
  }

  // 图表脚注样本量
  const capSkills = document.getElementById("cap-skills");
  if (capSkills) {
    capSkills.textContent = `数据来源：${meta.source_note || "公开招聘岗位"}，n=${meta.sample_size || 0} ｜ 一个岗位可提及多项技能`;
  }

  const footerSource = document.getElementById("footer-source");
  if (footerSource) {
    footerSource.textContent = `数据来源：${meta.source_note || "公开岗位样本"}（当前视图样本量 n=${meta.sample_size || 0}，全库 ${meta.total_in_db || 0} 份）。`;
  }

  renderCharts(data);
  renderCityTable(data.city_compare || []);
  renderTimeline(data.latest || []);
}

export function resizeAllCharts() {
  Object.values(chartInstances).forEach((inst) => {
    if (inst) inst.resize();
  });
}

export function rerenderChartsForTheme() {
  if (window.BriefCharts) {
    window.BriefCharts.register();
  }
  if (lastDashboardData) {
    Object.entries(chartInstances).forEach(([key, inst]) => {
      if (inst) {
        inst.dispose();
        chartInstances[key] = null;
      }
    });
    renderCharts(lastDashboardData);
  }
}
