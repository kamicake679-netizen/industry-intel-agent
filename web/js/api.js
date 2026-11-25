/**
 * Step 4 静态高保真稿专用 Mock 数据封装（不请求任何 /api/* 接口）。
 * Step 5 将在此文件内把导出函数替换为真实后端 API 调用，函数签名保持不变。
 */

const _mockCache = new Map();

/**
 * 加载 web/mock/ 下的静态 JSON 文件并缓存。
 * @param {string} name
 * @returns {Promise<any>}
 */
export async function loadMock(name) {
  if (_mockCache.has(name)) {
    return _mockCache.get(name);
  }
  const p = fetch(`/static/mock/${name}`).then((res) => {
    if (!res.ok) {
      throw new Error(`加载 Mock 数据失败: ${name} (${res.status})`);
    }
    return res.json();
  });
  _mockCache.set(name, p);
  return p;
}

/**
 * 获取指定城市的看板聚合数据（Mock）。
 * @param {string} city
 */
export async function getDashboard(city = "全部") {
  const all = await loadMock("dashboard.json");
  return all[city] || all["全部"];
}

/**
 * 获取指定城市的头条数据（Mock）。
 * @param {string} city
 */
export async function getHeadline(city = "全部") {
  const all = await loadMock("headline.json");
  return all[city] || all["全部"];
}

function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal && signal.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    const timer = setTimeout(resolve, ms);
    if (signal) {
      signal.addEventListener(
        "abort",
        () => {
          clearTimeout(timer);
          reject(new DOMException("Aborted", "AbortError"));
        },
        { once: true }
      );
    }
  });
}

/**
 * 模拟 SSE 流式问答：按 300ms 间隔逐步触发 node 与 final 事件。
 * @param {string} question
 * @param {(eventName: string, data: any) => Promise<void> | void} onEvent
 * @param {AbortSignal} [signal]
 */
export async function chatStreamMock(question, onEvent, signal) {
  const sample = await loadMock("chat_stream_sample.json");
  const scenarios = sample.scenarios || [];
  const qLower = (question || "").trim().toLowerCase();

  let matched = scenarios.find((sc) =>
    (sc.match || []).some((kw) => qLower.includes(kw.toLowerCase()))
  );
  if (!matched) {
    matched = scenarios[0];
  }

  for (const ev of matched.events || []) {
    if (signal && signal.aborted) {
      throw new DOMException("Aborted", "AbortError");
    }
    await sleep(300, signal);
    await onEvent(ev.event, ev.data);
  }
}

/**
 * 模拟 JD 结构化抽取（600ms 延迟）。
 * @param {string} rawText
 */
export async function extractMock(rawText) {
  await sleep(600);
  const text = (rawText || "").trim();
  const isGz = text.includes("广州");
  const isDg = text.includes("东莞");
  const city = isGz ? "广州" : isDg ? "东莞" : "深圳";
  const isFresh = text.includes("应届") || text.includes("经验不限") || text.includes("2026");

  return {
    is_new: true,
    job: {
      title: text.includes("Agent") ? "AI Agent 应用开发工程师" : "大模型应用工程师（RAG）",
      company: isGz ? "珠江智算科技" : "深湾智能实验室",
      city,
      district: isGz ? "天河区" : isDg ? "松山湖" : "南山区",
      category: "AI应用开发",
      salary_min: isFresh ? 18 : 25,
      salary_max: isFresh ? 28 : 40,
      salary_avg: isFresh ? 23.0 : 32.5,
      experience: isFresh ? "经验不限" : "1-3年",
      education: "本科",
      fresh_grad_friendly: isFresh,
      skills: ["Python", "LLM", "RAG", "LangGraph", "FastAPI"]
    }
  };
}

/**
 * 模拟生成今日简报 Markdown。
 * @param {string} city
 */
export async function briefMock(city = "全部") {
  const [dash, head] = await Promise.all([getDashboard(city), getHeadline(city)]);
  const kpi = dash.kpi || {};
  const topSkills = (dash.skills_top || []).slice(0, 5);
  const compare = dash.city_compare || [];

  const lines = [
    `# INDUSTRY INTEL · 今日行业情报简报（${city}）`,
    "",
    `> **${head.title || ""}**`,
    `> ${head.dek || ""}`,
    "",
    "## 一、核心指标",
    `- 收录岗位：**${kpi.total_jobs ?? 0}** 份（本周新增 +${kpi.weekly_new ?? 0}）`,
    `- 平均月薪：**${kpi.avg_salary ?? 0}K**（中位数 ${kpi.median_salary ?? 0}K，最高 ${kpi.max_salary ?? 0}K）`,
    `- 应届友好：**${kpi.fresh_grad_rate ?? 0}%**（${kpi.fresh_grad_count ?? 0} / ${kpi.total_jobs ?? 0} 份）`,
    "",
    "## 二、高频技能 TOP 5",
    ...topSkills.map((s, idx) => `${idx + 1}. **${s.skill}**：提及率 ${s.rate}%（${s.count} 岗）`),
    "",
    "## 三、三城横向对照",
    ...compare.map(
      (c) => `- **${c.city}**：${c.job_count} 岗 · 均薪 ${c.avg_salary}K · 应届友好 ${c.fresh_grad_rate}%`
    )
  ];

  return {
    mode: "preview",
    sent: false,
    platform: "none",
    status_code: null,
    markdown: lines.join("\n"),
    message: "静态稿：当前为预览模式，Step 5 将接入真实 Webhook 推送。"
  };
}
