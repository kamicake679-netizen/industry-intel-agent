/**
 * 头版看板渲染：报头、头条（含骨架屏）、KPI 平滑过渡、ECharts 图表复用、三城表高亮与最新入库时间线。
 */

const chartInstances = {
  skills: null,
  exp: null,
  category: null
};

let lastDashboardData = null;
let lastKpi = {
  total_jobs: 0,
  avg_salary: 0,
  fresh_grad_rate: 0,
  max_salary: 0
};
const recentSubmittedIds = new Set();
let highlightTimer = null;

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
 * 数字平滑计数动画（600ms，easeOutCubic），支持从旧值过渡到新值。
 */
function animateNumber(el, target, decimals = 0, suffixHtml = "", startVal = 0) {
  const endVal = Number(target) || 0;
  const fromVal = Number(startVal) || 0;
  if (prefersReducedMotion() || fromVal === endVal) {
    el.innerHTML = `${endVal.toFixed(decimals)}${suffixHtml}`;
    return;
  }

  const duration = 600;
  const startTime = performance.now();
  let finished = false;

  setTimeout(() => {
    if (!finished) {
      finished = true;
      el.innerHTML = `${endVal.toFixed(decimals)}${suffixHtml}`;
    }
  }, duration + 50);

  function frame(now) {
    if (finished) return;
    const progress = Math.min((now - startTime) / duration, 1);
    const eased = 1 - Math.pow(1 - progress, 3);
    const current = fromVal + (endVal - fromVal) * eased;
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

function formatIssueMeta(_updatedAt) {
  // 统一按中国标准时间（UTC+8）计算期号、日期与星期，避免海外时区偏差
  const utcMs = Date.now() + new Date().getTimezoneOffset() * 60000;
  const cnNow = new Date(utcMs + 8 * 3600000);
  const startOfYear = new Date(cnNow.getFullYear(), 0, 0);
  const dayOfYear = Math.floor((cnNow - startOfYear) / 86400000);
  const issueNo = String(dayOfYear).padStart(3, "0");

  const yyyy = cnNow.getFullYear();
  const mm = String(cnNow.getMonth() + 1).padStart(2, "0");
  const dd = String(cnNow.getDate()).padStart(2, "0");
  const weekNames = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
  const week = weekNames[cnNow.getDay()];

  const timeStr = "07:00";
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

function renderSkillsChart(skills, highlightIndex = 0) {
  const skillsChart = getOrCreateChart("skills", "chart-skills");
  if (!skillsChart) return;
  skillsChart.setOption(
    window.BriefCharts.barOption({
      labels: skills.map((s) => s.skill),
      values: skills.map((s) => s.rate),
      highlightIndex,
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

function renderCharts(data) {
  if (!window.echarts || !window.BriefCharts) return;

  // 图 1：技能提及率 TOP 10
  const skills = (data.skills_top || []).slice(0, 10);
  renderSkillsChart(skills, 0);

  // 图 2：经验—薪资阶梯
  const expList = data.exp_salary || [];
  const expChart = getOrCreateChart("exp", "chart-exp");
  if (expChart) {
    expChart.setOption(window.BriefCharts.ladderOption(expList), true);
    const el = document.getElementById("chart-exp");
    if (el && expList.length) {
      const first = expList[0];
      const last = expList[expList.length - 1];
      el.setAttribute(
        "aria-label",
        `经验与薪资阶梯柱状图：共 ${expList.length} 个经验分段，从 ${first.experience}（${Number(first.avg_salary).toFixed(1)}K）至 ${last.experience}（${Number(last.avg_salary).toFixed(1)}K）`
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
      return `<tr data-city="${escapeHtml(c.city)}">
        <td style="font-family:var(--sans)">${escapeHtml(c.city)}</td>
        <td class="num">${Number(c.job_count)}</td>
        <td class="num">${Number(c.avg_salary || 0).toFixed(1)}<span class="num-unit">K</span></td>
        <td class="num ${isTopFresh ? "is-highlight" : ""}">${Number(c.fresh_grad_rate || 0).toFixed(1)}<span class="num-unit">%</span></td>
      </tr>`;
    })
    .join("");

  const sortedByFresh = [...compare].sort((a, b) => b.fresh_grad_rate - a.fresh_grad_rate);
  const best = sortedByFresh[0];
  const lowest = sortedByFresh[sortedByFresh.length - 1];
  const ratio = lowest.fresh_grad_rate > 0 ? (best.fresh_grad_rate / lowest.fresh_grad_rate).toFixed(1) : "1.0";

  noteEl.innerHTML = `<span class="editor-note-lead">编者按　</span>${escapeHtml(best.city)}的应届友好岗位占比（<span class="mono">${Number(best.fresh_grad_rate).toFixed(1)}%</span>）最高，是${escapeHtml(lowest.city)}（<span class="mono">${Number(lowest.fresh_grad_rate).toFixed(1)}%</span>）的 <span class="mono">${ratio}</span> 倍，零经验转行者可优先关注。`;
}

function buildTimelineItemHtml(item) {
  const isNew = item.source === "user_submit" || recentSubmittedIds.has(item.id);
  const minK = item.salary_min != null ? Number(item.salary_min).toFixed(0) : "";
  const maxK = item.salary_max != null ? Number(item.salary_max).toFixed(0) : "";
  const salaryHtml = minK && maxK
    ? `${escapeHtml(minK)}–${escapeHtml(maxK)}<span class="num-unit">K</span>`
    : `${escapeHtml(Number(item.salary_avg || 0).toFixed(1))}<span class="num-unit">K</span>`;
  const timeText = String(item.created_at || "").slice(5, 16) || "刚刚";
  const skillsText = (item.skills || []).slice(0, 5).join(" · ") || "—";

  return `<li class="timeline-item ${isNew ? "is-new" : ""}" data-job-id="${escapeHtml(item.id ?? "")}">
    <div class="timeline-top">
      <div class="timeline-title-wrap">
        <span class="timeline-job-title">${escapeHtml(item.title)}</span>
        ${isNew ? '<span class="badge-new">新</span>' : ""}
      </div>
      <span class="mono timeline-salary">${salaryHtml}</span>
    </div>
    <div class="timeline-meta">
      <span class="mono">${escapeHtml(timeText)}</span> · ${escapeHtml(item.company || "匿名企业")} · ${escapeHtml(item.city || "大湾区")}${item.district ? " " + escapeHtml(item.district) : ""} · ${escapeHtml(item.experience || "经验不限")}
    </div>
    <div class="timeline-skills">技能：${escapeHtml(skillsText)}</div>
  </li>`;
}

function renderTimeline(latest) {
  const listEl = document.getElementById("latest-timeline");
  const countEl = document.getElementById("timeline-count");
  if (!listEl) return;

  const items = (latest || []).slice(0, 8);
  if (countEl) {
    countEl.textContent = `最近 ${items.length} 条`;
  }

  if (!items.length) {
    listEl.innerHTML = '<li class="timeline-item"><div class="timeline-meta">暂无入库岗位数据</div></li>';
    return;
  }

  listEl.innerHTML = items.map(buildTimelineItemHtml).join("");
}

/**
 * 在时间线顶部插入新投稿的 JD（带朱砂“新”标记）。
 * @param {object} job
 */
export function prependSubmittedJob(job) {
  if (!job) return;
  if (job.id != null) {
    recentSubmittedIds.add(job.id);
  }
  const listEl = document.getElementById("latest-timeline");
  const countEl = document.getElementById("timeline-count");
  if (!listEl) return;

  const existing = job.id != null ? listEl.querySelector(`[data-job-id="${job.id}"]`) : null;
  if (existing) {
    existing.remove();
  }

  const wrapper = document.createElement("div");
  wrapper.innerHTML = buildTimelineItemHtml({ ...job, source: "user_submit" });
  const newLi = wrapper.firstElementChild;
  if (newLi) {
    listEl.prepend(newLi);
    while (listEl.children.length > 8) {
      listEl.lastElementChild.remove();
    }
    if (countEl) {
      countEl.textContent = `最近 ${listEl.children.length} 条`;
    }
  }
}

/**
 * 头条骨架屏占位（排版待印细条，不用旋转圈）。
 * @param {boolean} isLoading
 */
export function setHeadlineLoading(isLoading) {
  const titleEl = document.getElementById("headline-title");
  const dekEl = document.getElementById("headline-dek");
  if (!titleEl || !dekEl) return;
  if (isLoading) {
    titleEl.innerHTML = '<div class="skeleton-bar skeleton-headline-title" aria-hidden="true"></div>';
    dekEl.innerHTML = '<div class="skeleton-bar skeleton-headline-dek" aria-hidden="true"></div>';
  }
}

/**
 * 看板加载态骨架占位（印刷待排细条，首次加载时展示）。
 * @param {boolean} isLoading
 */
export function setDashboardLoading(isLoading) {
  setHeadlineLoading(isLoading);
  if (!isLoading) return;
  if (!lastDashboardData) {
    ["kpi-total", "kpi-avg", "kpi-fresh", "kpi-max"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.innerHTML = '<div class="skeleton-bar skeleton-kpi" aria-hidden="true"></div>';
    });
    const listEl = document.getElementById("latest-timeline");
    if (listEl) {
      listEl.innerHTML = [1, 2, 3]
        .map(() => '<li class="timeline-item"><div class="skeleton-bar skeleton-line" aria-hidden="true"></div></li>')
        .join("");
    }
  }
}

/**
 * 渲染头条区块。
 * @param {object} headline
 */
export function renderHeadline(headline) {
  const kickerEl = document.getElementById("headline-kicker");
  const titleEl = document.getElementById("headline-title");
  const dekEl = document.getElementById("headline-dek");

  if (kickerEl && headline?.kicker) kickerEl.textContent = headline.kicker;
  if (titleEl && headline?.title) titleEl.textContent = headline.title;
  if (dekEl) dekEl.textContent = headline?.dek || "";
}

/**
 * 渲染完整头版看板（图表使用 setOption 平滑过渡，KPI 从旧值平滑计数）。
 */
export function renderDashboard(data, headline = null) {
  lastDashboardData = data;
  const kpi = data.kpi || {};
  const meta = data.meta || {};

  const issueEl = document.getElementById("masthead-issue");
  if (issueEl) {
    issueEl.textContent = formatIssueMeta(meta.updated_at);
  }

  if (headline) {
    renderHeadline(headline);
  }

  const hasDemo = Boolean(meta.has_demo_data);
  const demoNoteEl = document.getElementById("headline-demo-note");
  const footerDemoEl = document.getElementById("footer-demo-note");
  if (demoNoteEl) demoNoteEl.classList.toggle("hidden", !hasDemo);
  if (footerDemoEl) footerDemoEl.classList.toggle("hidden", !hasDemo);

  // KPI 四栏（从上一次数值平滑 count-up）
  const totalEl = document.getElementById("kpi-total");
  const weeklyEl = document.getElementById("kpi-weekly");
  const avgEl = document.getElementById("kpi-avg");
  const medianEl = document.getElementById("kpi-median");
  const freshEl = document.getElementById("kpi-fresh");
  const freshCntEl = document.getElementById("kpi-fresh-count");
  const maxEl = document.getElementById("kpi-max");
  const maxJobEl = document.getElementById("kpi-max-job");

  const nextTotal = Number(kpi.total_jobs || 0);
  const nextAvg = Number(kpi.avg_salary || 0);
  const nextFresh = Number(kpi.fresh_grad_rate || 0);
  const nextMax = Number(kpi.max_salary || 0);

  if (totalEl) animateNumber(totalEl, nextTotal, 0, "", lastKpi.total_jobs);
  if (weeklyEl) weeklyEl.innerHTML = `▲ 本周 <span class="mono">+${Number(kpi.weekly_new || 0)}</span>`;
  if (avgEl) animateNumber(avgEl, nextAvg, 1, '<span class="kpi-unit">K</span>', lastKpi.avg_salary);
  if (medianEl) medianEl.innerHTML = `中位数 <span class="mono">${Number(kpi.median_salary || 0).toFixed(1)}<span class="num-unit">K</span></span>`;
  if (freshEl) animateNumber(freshEl, nextFresh, 1, '<span class="kpi-unit">%</span>', lastKpi.fresh_grad_rate);
  if (freshCntEl) freshCntEl.innerHTML = `<span class="mono">${Number(kpi.fresh_grad_count || 0)} / ${Number(kpi.total_jobs || 0)}</span> 份`;
  if (maxEl) animateNumber(maxEl, nextMax, 1, '<span class="kpi-unit">K</span>', lastKpi.max_salary);
  if (maxJobEl) {
    maxJobEl.textContent = kpi.max_salary_job
      ? `${kpi.max_salary_job.city} · ${kpi.max_salary_job.title}`
      : "—";
  }

  lastKpi = {
    total_jobs: nextTotal,
    avg_salary: nextAvg,
    fresh_grad_rate: nextFresh,
    max_salary: nextMax
  };

  // 每张图表与对照表的出处与样本量脚注
  const sourceNote = meta.source_note || "公开招聘岗位";
  const sampleN = Number(meta.sample_size || 0);
  const totalN = Number(meta.total_in_db || sampleN);

  const capSkills = document.getElementById("cap-skills");
  if (capSkills) {
    capSkills.textContent = `数据来源：${sourceNote}，n=${sampleN} ｜ 一个岗位可提及多项技能`;
  }
  const capCity = document.getElementById("cap-city");
  if (capCity) {
    capCity.textContent = `数据来源：${sourceNote}，全库 n=${totalN} ｜ 广深莞三城在招岗位对照`;
  }
  const capExp = document.getElementById("cap-exp");
  if (capExp) {
    capExp.textContent = `数据来源：${sourceNote}，n=${sampleN} ｜ 按月薪均值（K）统计，不含年终奖`;
  }
  const capCategory = document.getElementById("cap-category");
  if (capCategory) {
    capCategory.textContent = `数据来源：${sourceNote}，n=${sampleN} ｜ 前五类别单列，其余合并为“其他”`;
  }

  const footerSource = document.getElementById("footer-source");
  if (footerSource) {
    footerSource.textContent = `数据来源：${sourceNote}（当前视图样本量 n=${sampleN}，全库 ${totalN} 份）。`;
  }

  renderCharts(data);
  renderCityTable(data.city_compare || []);
  renderTimeline(data.latest || []);
}

export function getCurrentDashboardSnapshot() {
  return lastDashboardData;
}

/**
 * 在左侧看板中高亮指定技能柱或城市行，持续 2 秒后自动恢复。
 * @param {{ city?: string | null, skill?: string | null }} target
 */
export function highlightDashboardTarget({ city = null, skill = null } = {}) {
  if (!lastDashboardData) return;
  if (highlightTimer) {
    clearTimeout(highlightTimer);
    highlightTimer = null;
  }

  let scrolled = false;
  const skills = (lastDashboardData.skills_top || []).slice(0, 10);

  if (skill) {
    const idx = skills.findIndex((s) => s.skill.toLowerCase() === String(skill).toLowerCase());
    if (idx !== -1) {
      renderSkillsChart(skills, idx);
      const chartEl = document.getElementById("chart-skills");
      if (chartEl && !scrolled) {
        chartEl.scrollIntoView({ behavior: "smooth", block: "center" });
        scrolled = true;
      }
    }
  }

  const flashedRows = [];
  if (city) {
    const row = document.querySelector(`#city-compare-body tr[data-city="${CSS.escape(city)}"]`);
    if (row) {
      row.classList.add("is-flash-highlight");
      flashedRows.push(row);
      if (!scrolled) {
        row.scrollIntoView({ behavior: "smooth", block: "center" });
        scrolled = true;
      }
    }
  }

  highlightTimer = setTimeout(() => {
    renderSkillsChart(skills, 0);
    flashedRows.forEach((r) => r.classList.remove("is-flash-highlight"));
    highlightTimer = null;
  }, 2000);
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
