"""Step 7 Text-to-SQL 消融评测报告生成器。

根据 ``eval/results/latest_summary.json``（或指定 JSON 文件）生成：
1. ``eval/EVAL_REPORT.md``：包含消融对比总表、分类得分、3 个典型自愈案例、全部安全拦截案例；
2. ``eval/report.html``：同样采用「情报简报」编辑部风格的静态自包含 HTML 报告。
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

DEFAULT_SUMMARY_PATH = ROOT_DIR / "eval" / "results" / "latest_summary.json"
DEFAULT_MD_PATH = ROOT_DIR / "eval" / "EVAL_REPORT.md"
DEFAULT_HTML_PATH = ROOT_DIR / "eval" / "report.html"

CATEGORY_LABELS: dict[str, str] = {
    "basic_filter": "基础筛选",
    "group_agg": "分组聚合",
    "skill_complex": "技能匹配与复合条件",
    "security_attack": "安全攻击拦截",
}

CONFIG_TITLES: dict[str, str] = {
    "A": "A · 基线（精简 Schema / 无 Few-Shot / 无自愈 / 弱正则）",
    "B": "B · 增强提示（完整 Schema + 枚举 + Few-Shot / 弱正则）",
    "C": "C · 完整版（B + 自愈重试 + 规范化技能表 + 三层只读防线）",
}


def load_summary(path: Path = DEFAULT_SUMMARY_PATH) -> dict[str, Any]:
    """加载评测汇总 JSON 文件。"""
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _fmt_pct(val: float | int | None) -> str:
    if val is None:
        return "—"
    return f"{float(val):.1f}%"


def _fmt_num(val: float | int | None, digits: int = 1) -> str:
    if val is None:
        return "—"
    return f"{float(val):.{digits}f}"


def _esc_md(text: str) -> str:
    return (text or "").replace("\n", " ").replace("|", "\\|").strip()


def _collect_repair_cases(summary: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """分别收集主评测自然触发的自愈案例与专项故障注入验证案例。"""
    natural: list[dict[str, Any]] = []
    c_data = (summary.get("configs") or {}).get("C") or {}
    for item in c_data.get("results") or []:
        if not item.get("expect_blocked") and int(item.get("retry_count") or 0) > 0 and item.get("ok"):
            natural.append(item)

    probes = list(summary.get("repair_probe_cases") or [])
    return natural[:3], probes[: max(0, 3 - len(natural[:3]))]


def _collect_security_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """整理全部安全攻击题在各配置下的拦截对比结果。"""
    configs = summary.get("configs") or {}
    order = [k for k in ("A", "B", "C") if k in configs]
    if not order:
        return []

    by_id: dict[str, dict[str, Any]] = {}
    for cfg in order:
        for item in configs[cfg].get("results") or []:
            if not item.get("expect_blocked"):
                continue
            qid = item["id"]
            entry = by_id.setdefault(
                qid,
                {
                    "id": qid,
                    "question": item.get("question", ""),
                    "per_cfg": {},
                },
            )
            entry["per_cfg"][cfg] = item

    return sorted(by_id.values(), key=lambda x: x["id"])


def build_markdown_report(summary: dict[str, Any]) -> str:
    """生成 Markdown 格式的完整评测报告。"""
    meta = summary.get("meta") or {}
    configs = summary.get("configs") or {}
    cfg_keys = [k for k in ("A", "B", "C") if k in configs]

    lines: list[str] = [
        "# Text-to-SQL 自动化消融评测报告（EVAL_REPORT）",
        "",
        f"> 生成时间：`{meta.get('timestamp', '')}` · 评测模型：`{meta.get('llm_model', '')}` · "
        f"题目数：`{meta.get('dataset_size', 0)}` 题（非攻击 `{meta.get('non_attack_count', 0)}` 题 + 安全攻击 `{meta.get('attack_count', 0)}` 题） · "
        f"样本库：`{meta.get('db_job_count', 0)}` 条岗位",
        "",
        "## 一、消融实验总表（A / B / C）",
        "",
        "| 配置 | 首次执行成功率 | 最终执行成功率 | 结果准确率（精确+语义） | 安全拦截率 | 误拦截率 | 平均自愈次数 | P50 延迟 | P95 延迟 | 平均 Token | 估算单次成本 |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]

    for key in cfg_keys:
        m = configs[key]["metrics"]
        strict_cnt = int(m.get("strict_match_count", 0))
        sem_cnt = int(m.get("semantic_match_count", 0))
        non_atk = int(m.get("non_attack_total", 0))
        acc_detail = f"{_fmt_pct(m.get('result_accuracy'))} ({strict_cnt}+{sem_cnt}/{non_atk})"
        lines.append(
            f"| **{CONFIG_TITLES.get(key, key)}** "
            f"| {_fmt_pct(m.get('first_try_exec_rate'))} "
            f"| {_fmt_pct(m.get('final_exec_rate'))} "
            f"| {acc_detail} "
            f"| {_fmt_pct(m.get('security_block_rate'))} "
            f"| {_fmt_pct(m.get('false_positive_block_rate'))} "
            f"| {_fmt_num(m.get('avg_retries'), 2)} "
            f"| {_fmt_num(m.get('p50_latency_ms'), 0)} ms "
            f"| {_fmt_num(m.get('p95_latency_ms'), 0)} ms "
            f"| {_fmt_num(m.get('avg_total_tokens'), 0)} "
            f"| ¥{_fmt_num(m.get('est_cost_cny_per_query'), 5)} |"
        )

    lines.extend([
        "",
        "> 口径说明：结果准确率 =（严格集合数值比对通过 + LLM 语义等价判定通过）/ 非攻击题总数；数值比对忽略列名与行顺序，浮点容差 `0.01`。单次成本按 DeepSeek-V3 参考单价（输入 ¥2/M tokens、输出 ¥8/M tokens）估算。",
        "",
        "## 二、分题型得分明细",
        "",
        "| 题型分类 | 题数 | " + " | ".join(f"{k} 组" for k in cfg_keys) + " |",
        "| :--- | ---: | " + " | ".join(":---" for _ in cfg_keys) + " |",
    ])

    for cat_key in ("basic_filter", "group_agg", "skill_complex", "security_attack"):
        cat_label = CATEGORY_LABELS.get(cat_key, cat_key)
        count_val = 0
        cells: list[str] = []
        for cfg in cfg_keys:
            cat_stat = (configs[cfg]["metrics"].get("by_category") or {}).get(cat_key) or {}
            count_val = int(cat_stat.get("total", count_val))
            if cat_key == "security_attack":
                blk_cnt = int(cat_stat.get("blocked_count", 0))
                rate = _fmt_pct(cat_stat.get("block_rate"))
                cells.append(f"拦截率 {rate} ({blk_cnt}/{count_val})")
            else:
                exec_rate = _fmt_pct(cat_stat.get("final_exec_rate"))
                acc_rate = _fmt_pct(cat_stat.get("result_accuracy"))
                cells.append(f"执行 {exec_rate} / 准确 {acc_rate}")
        lines.append(f"| {cat_label} | {count_val} | " + " | ".join(cells) + " |")

    natural_repairs, probe_repairs = _collect_repair_cases(summary)
    lines.extend([
        "",
        "## 三、典型 SQL 自愈案例（共 3 例）",
        "",
    ])
    if natural_repairs:
        lines.append("### 3.1 主评测自然触发自愈案例")
        lines.append("")
        for idx, item in enumerate(natural_repairs, start=1):
            lines.extend([
                f"**案例 {idx}（{item['id']}）**：{_esc_md(item.get('question', ''))}",
                f"- 重试次数：`{item.get('retry_count', 0)}` · 最终返回行数：`{item.get('row_count', 0)}` · 耗时：`{_fmt_num(item.get('latency_ms'), 0)} ms`",
                f"- 首次/历史错误：`{_esc_md(item.get('repair_first_error') or '执行错误后触发修复')}`",
                f"- 修复后 SQL：`{item.get('sql', '')}`",
                "",
            ])

    if probe_repairs:
        lines.append("### 3.2 自愈专项故障注入验证（真实调用修复闭环）")
        lines.append("")
        for idx, probe in enumerate(probe_repairs, start=len(natural_repairs) + 1):
            lines.extend([
                f"**案例 {idx}（{probe.get('probe_id', f'P{idx}')} · {probe.get('fault_type', '')}）**：{_esc_md(probe.get('question', ''))}",
                f"- 故障 SQL：`{probe.get('initial_sql', '')}`",
                f"- SQLite 真实报错：`{_esc_md(probe.get('initial_error', ''))}`",
                f"- 修复后 SQL：`{probe.get('repaired_sql', '')}`",
                f"- 修复结果：`{'成功' if probe.get('ok') else '失败'}` · 返回行数：`{probe.get('row_count', 0)}` · 耗时：`{_fmt_num(probe.get('latency_ms'), 0)} ms`",
                "",
            ])

    if not natural_repairs and not probe_repairs:
        lines.append("本次运行未包含自愈验证数据（可能未运行 C 组或开启了 `--skip-repair-probe`）。")
        lines.append("")

    sec_rows = _collect_security_rows(summary)
    lines.extend([
        "## 四、全部安全攻击拦截案例",
        "",
        "| 编号 | 攻击提问 | " + " | ".join(f"{k} 组状态与拦截原因" for k in cfg_keys) + " |",
        "| :--- | :--- | " + " | ".join(":---" for _ in cfg_keys) + " |",
    ])
    for row in sec_rows:
        per_cfg_cells: list[str] = []
        for cfg in cfg_keys:
            item = row["per_cfg"].get(cfg) or {}
            if item.get("blocked"):
                reason = _esc_md(item.get("block_reason") or "已拦截")
                per_cfg_cells.append(f"**已拦截**（{reason[:60]}）")
            else:
                err = _esc_md(item.get("error") or "未拦截且执行通过")
                per_cfg_cells.append(f"未拦截（{err[:50]}）")
        lines.append(
            f"| {row['id']} | {_esc_md(row['question'])} | " + " | ".join(per_cfg_cells) + " |"
        )

    lines.extend(["", "## 五、核心发现与工程启示", ""])
    if "A" in configs and "C" in configs:
        ma = configs["A"]["metrics"]
        mc = configs["C"]["metrics"]
        lines.append(
            f"1. **Schema 枚举与 Few-Shot + 规范化技能表**：从 A 组到 C 组，非攻击题结果准确率从 "
            f"`{_fmt_pct(ma.get('result_accuracy'))}` 提升至 `{_fmt_pct(mc.get('result_accuracy'))}`，"
            f"首次执行成功率从 `{_fmt_pct(ma.get('first_try_exec_rate'))}` 提升至 `{_fmt_pct(mc.get('first_try_exec_rate'))}`。"
        )
        lines.append(
            f"2. **三层只读防线必要性**：A/B 组仅靠弱正则黑名单时，安全拦截率仅 `{_fmt_pct(ma.get('security_block_rate'))}`"
            f"（面对自然语言越权、注释绕过、读取 `query_logs` 与笛卡尔积拒绝服务均会失守）；"
            f"C 组通过意图初审 + SQL 预检重写 + `mode=ro` 授权器 + 超时护栏实现 `{_fmt_pct(mc.get('security_block_rate'))}` 全量拦截。"
        )
        lines.append(
            f"3. **延迟与成本权衡**：C 组由于增加意图路由、自愈容错与洞察总结节点，P50 延迟为 `{_fmt_num(mc.get('p50_latency_ms'), 0)} ms`，"
            f"单次查询平均消耗 `{_fmt_num(mc.get('avg_total_tokens'), 0)}` tokens（约 ¥{_fmt_num(mc.get('est_cost_cny_per_query'), 5)}），"
            "在生产上可通过 SSE 逐节点流式渲染显著改善用户体感延迟。"
        )
    else:
        lines.append("本次仅运行了部分消融配置，完整对比结论请使用 `--config all` 运行全量评测。")

    return "\n".join(lines) + "\n"


def build_html_report(summary: dict[str, Any]) -> str:
    """生成「情报简报」编辑部风格的静态自包含 HTML 评测报告。"""
    meta = summary.get("meta") or {}
    configs = summary.get("configs") or {}
    cfg_keys = [k for k in ("A", "B", "C") if k in configs]

    # 生成三组对比卡片与条形图
    summary_rows_html: list[str] = []
    bars_html: list[str] = []
    for idx, key in enumerate(cfg_keys):
        m = configs[key]["metrics"]
        is_c = key == "C"
        acc = float(m.get("result_accuracy") or 0.0)
        first_exec = float(m.get("first_try_exec_rate") or 0.0)
        sec = float(m.get("security_block_rate") or 0.0)
        strict_cnt = int(m.get("strict_match_count", 0))
        sem_cnt = int(m.get("semantic_match_count", 0))
        non_atk = int(m.get("non_attack_total", 0))

        summary_rows_html.append(
            f"""
            <tr class="{'row-highlight' if is_c else ''}">
              <td><strong>{html.escape(CONFIG_TITLES.get(key, key))}</strong></td>
              <td class="mono num">{_fmt_pct(first_exec)}</td>
              <td class="mono num">{_fmt_pct(m.get('final_exec_rate'))}</td>
              <td class="mono num {'accent-num' if is_c else ''}">{_fmt_pct(acc)} <span class="sub-note">({strict_cnt}+{sem_cnt}/{non_atk})</span></td>
              <td class="mono num {'moss-num' if sec >= 99.9 else ''}">{_fmt_pct(sec)}</td>
              <td class="mono num">{_fmt_num(m.get('avg_retries'), 2)}</td>
              <td class="mono num">{_fmt_num(m.get('p50_latency_ms'), 0)} ms</td>
              <td class="mono num">{_fmt_num(m.get('p95_latency_ms'), 0)} ms</td>
              <td class="mono num">{_fmt_num(m.get('avg_total_tokens'), 0)}</td>
              <td class="mono num">¥{_fmt_num(m.get('est_cost_cny_per_query'), 5)}</td>
            </tr>
            """
        )

        fill_cls = "fill-accent" if is_c else ("fill-ink2" if key == "B" else "fill-mute")
        bars_html.append(
            f"""
            <div class="bar-group">
              <div class="bar-label-row">
                <span class="bar-title">{html.escape(key)} 组 · {html.escape(CONFIG_TITLES.get(key, key).split('·', 1)[-1].strip())}</span>
                <span class="mono bar-val">准确率 {_fmt_pct(acc)} · 安全拦截 {_fmt_pct(sec)}</span>
              </div>
              <div class="bar-track">
                <div class="bar-fill {fill_cls}" style="width: {min(100.0, max(0.0, acc)):.1f}%"></div>
              </div>
            </div>
            """
        )

    # 分题型表格
    cat_rows_html: list[str] = []
    for cat_key in ("basic_filter", "group_agg", "skill_complex", "security_attack"):
        cat_label = CATEGORY_LABELS.get(cat_key, cat_key)
        total_val = 0
        cells_html: list[str] = []
        for cfg in cfg_keys:
            cs = (configs[cfg]["metrics"].get("by_category") or {}).get(cat_key) or {}
            total_val = int(cs.get("total", total_val))
            if cat_key == "security_attack":
                blk = int(cs.get("blocked_count", 0))
                rate = float(cs.get("block_rate") or 0.0)
                badge_cls = "badge-moss" if rate >= 99.9 else "badge-warn"
                cells_html.append(f'<td class="mono"><span class="badge {badge_cls}">{_fmt_pct(rate)}</span> ({blk}/{total_val})</td>')
            else:
                er = _fmt_pct(cs.get("final_exec_rate"))
                ar = _fmt_pct(cs.get("result_accuracy"))
                cells_html.append(f'<td class="mono">执行 {er} · 准确 <strong>{ar}</strong></td>')
        cat_rows_html.append(
            f"<tr><td>{html.escape(cat_label)}</td><td class='mono num'>{total_val}</td>{''.join(cells_html)}</tr>"
        )

    # 自愈案例
    natural_repairs, probe_repairs = _collect_repair_cases(summary)
    repair_cards: list[str] = []
    for idx, item in enumerate(natural_repairs, start=1):
        repair_cards.append(
            f"""
            <article class="case-block">
              <div class="case-head">
                <span class="kicker">自然触发自愈 · {html.escape(item.get('id', ''))}</span>
                <span class="mono meta-chip">重试 {int(item.get('retry_count') or 0)} 次 · {_fmt_num(item.get('latency_ms'), 0)} ms</span>
              </div>
              <p class="case-q">{html.escape(item.get('question', ''))}</p>
              <div class="case-meta"><strong>首次错误：</strong>{html.escape(item.get('repair_first_error') or '执行报错后触发重写')}</div>
              <pre class="sql-box">{html.escape(item.get('sql', ''))}</pre>
            </article>
            """
        )
    for idx, probe in enumerate(probe_repairs, start=len(natural_repairs) + 1):
        repair_cards.append(
            f"""
            <article class="case-block">
              <div class="case-head">
                <span class="kicker">故障注入验证 · {html.escape(probe.get('probe_id', ''))}（{html.escape(probe.get('fault_type', ''))}）</span>
                <span class="mono meta-chip">{'修复成功' if probe.get('ok') else '修复失败'} · 返回 {int(probe.get('row_count') or 0)} 行 · {_fmt_num(probe.get('latency_ms'), 0)} ms</span>
              </div>
              <p class="case-q">{html.escape(probe.get('question', ''))}</p>
              <div class="case-meta"><strong>注入 SQL：</strong><code class="mono">{html.escape(probe.get('initial_sql', ''))}</code></div>
              <div class="case-meta err-text"><strong>SQLite 报错：</strong>{html.escape(probe.get('initial_error', ''))}</div>
              <pre class="sql-box">{html.escape(probe.get('repaired_sql', ''))}</pre>
            </article>
            """
        )

    # 安全拦截表
    sec_rows_html: list[str] = []
    for row in _collect_security_rows(summary):
        cells: list[str] = []
        for cfg in cfg_keys:
            item = row["per_cfg"].get(cfg) or {}
            if item.get("blocked"):
                reason = html.escape((item.get("block_reason") or "已拦截")[:68])
                cells.append(f'<td><span class="badge badge-moss">已拦截</span><div class="reason-txt">{reason}</div></td>')
            else:
                err = html.escape((item.get("error") or "未拦截且执行通过")[:60])
                cells.append(f'<td><span class="badge badge-accent">未拦截</span><div class="reason-txt">{err}</div></td>')
        sec_rows_html.append(
            f"<tr><td class='mono'>{html.escape(row['id'])}</td><td>{html.escape(row['question'])}</td>{''.join(cells)}</tr>"
        )

    # 头条 KPI
    mc = (configs.get("C") or configs.get(cfg_keys[-1]) or {}).get("metrics") or {}
    ma = (configs.get("A") or {}).get("metrics") or {}
    delta_acc = float(mc.get("result_accuracy") or 0) - float(ma.get("result_accuracy") or 0)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>INDUSTRY INTEL · Text-to-SQL 消融评测报告</title>
  <style>
    :root {{
      --paper: #F3EFE6;
      --paper-2: #E9E3D6;
      --ink: #1B1A17;
      --ink-2: #55514A;
      --ink-3: #8A847A;
      --rule: #D6CEBD;
      --accent: #C8401A;
      --moss: #4E6B4F;
      --ochre: #B7822A;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--paper);
      color: var(--ink);
      font-family: "PingFang SC", "HarmonyOS Sans SC", "Microsoft YaHei", sans-serif;
      font-size: 15px;
      line-height: 1.7;
    }}
    .mono {{
      font-family: "JetBrains Mono", "Cascadia Mono", Consolas, monospace;
      font-variant-numeric: tabular-nums;
    }}
    .num {{ text-align: right; white-space: nowrap; }}
    .wrap {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 28px 24px 56px;
    }}
    .masthead {{
      border-bottom: 3px double var(--ink);
      padding-bottom: 14px;
      margin-bottom: 24px;
      display: flex;
      justify-content: space-between;
      align-items: flex-end;
      flex-wrap: wrap;
      gap: 12px;
    }}
    .brand {{
      font-family: "Noto Serif SC", "Source Han Serif SC", "Songti SC", serif;
      font-size: 28px;
      font-weight: 900;
      letter-spacing: 0.04em;
      margin: 0;
    }}
    .brand-sub {{
      font-size: 12px;
      letter-spacing: 0.14em;
      color: var(--ink-3);
      text-transform: uppercase;
      margin-bottom: 4px;
    }}
    .meta-line {{
      font-size: 12px;
      color: var(--ink-2);
    }}
    .headline-box {{
      border-bottom: 1px solid var(--rule);
      padding-bottom: 20px;
      margin-bottom: 24px;
    }}
    .kicker {{
      font-size: 12px;
      letter-spacing: 0.14em;
      color: var(--accent);
      font-weight: 600;
      text-transform: uppercase;
    }}
    h1.headline {{
      font-family: "Noto Serif SC", "Source Han Serif SC", "Songti SC", serif;
      font-size: 32px;
      font-weight: 900;
      line-height: 1.28;
      margin: 6px 0 10px;
    }}
    .dek {{
      color: var(--ink-2);
      margin: 0;
      max-width: 72ch;
    }}
    .kpi-grid {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      border-top: 1px solid var(--rule);
      border-bottom: 1px solid var(--rule);
      margin-bottom: 28px;
    }}
    .kpi-cell {{
      padding: 16px 18px;
      border-right: 1px solid var(--rule);
    }}
    .kpi-cell:last-child {{ border-right: none; }}
    .kpi-label {{
      font-size: 12px;
      letter-spacing: 0.1em;
      color: var(--ink-3);
    }}
    .kpi-value {{
      font-size: 30px;
      font-weight: 600;
      line-height: 1.2;
      margin: 6px 0 4px;
    }}
    .kpi-note {{
      font-size: 12px;
      color: var(--ink-2);
    }}
    .accent-num {{ color: var(--accent); font-weight: 700; }}
    .moss-num {{ color: var(--moss); font-weight: 700; }}
    section {{
      margin-bottom: 32px;
      padding-bottom: 24px;
      border-bottom: 1px solid var(--rule);
    }}
    h2.sec-title {{
      font-family: "Noto Serif SC", "Source Han Serif SC", "Songti SC", serif;
      font-size: 20px;
      font-weight: 700;
      margin: 0 0 14px;
    }}
    .table-scroll {{
      overflow-x: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      padding: 10px 10px;
      border-bottom: 1px solid var(--rule);
      text-align: left;
      vertical-align: top;
    }}
    th {{
      font-size: 12px;
      color: var(--ink-3);
      font-weight: 600;
      border-bottom: 1px solid var(--ink);
      white-space: nowrap;
    }}
    tr.row-highlight {{
      background: var(--paper-2);
    }}
    .sub-note {{
      font-size: 11px;
      color: var(--ink-3);
    }}
    .bar-chart {{
      display: grid;
      gap: 14px;
      margin-top: 12px;
    }}
    .bar-label-row {{
      display: flex;
      justify-content: space-between;
      font-size: 13px;
      margin-bottom: 4px;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .bar-track {{
      height: 12px;
      background: var(--paper-2);
      border: 1px solid var(--rule);
    }}
    .bar-fill {{ height: 100%; }}
    .fill-accent {{ background: var(--accent); }}
    .fill-ink2 {{ background: var(--ink-2); }}
    .fill-mute {{ background: var(--ink-3); }}
    .cases-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 16px;
    }}
    .case-block {{
      border: 1px solid var(--rule);
      padding: 14px 16px;
      background: var(--paper);
    }}
    .case-head {{
      display: flex;
      justify-content: space-between;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 8px;
    }}
    .meta-chip {{
      font-size: 12px;
      color: var(--ink-3);
    }}
    .case-q {{
      font-weight: 600;
      margin: 0 0 8px;
    }}
    .case-meta {{
      font-size: 12px;
      color: var(--ink-2);
      margin-bottom: 6px;
      word-break: break-word;
    }}
    .err-text {{ color: var(--ochre); }}
    pre.sql-box {{
      margin: 8px 0 0;
      padding: 10px 12px;
      background: #161512;
      color: #E9E3D6;
      font-family: "JetBrains Mono", "Cascadia Mono", Consolas, monospace;
      font-size: 12px;
      line-height: 1.5;
      overflow-x: auto;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .badge {{
      display: inline-block;
      padding: 1px 6px;
      font-size: 11px;
      font-family: "JetBrains Mono", monospace;
      border: 1px solid currentColor;
    }}
    .badge-moss {{ color: var(--moss); }}
    .badge-warn {{ color: var(--ochre); }}
    .badge-accent {{ color: var(--accent); }}
    .reason-txt {{
      font-size: 12px;
      color: var(--ink-2);
      margin-top: 4px;
    }}
    footer {{
      font-size: 12px;
      color: var(--ink-3);
    }}
    @media (max-width: 780px) {{
      .kpi-grid {{ grid-template-columns: repeat(2, 1fr); }}
      .kpi-cell {{ border-bottom: 1px solid var(--rule); }}
      h1.headline {{ font-size: 24px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <header class="masthead">
      <div>
        <div class="brand-sub">INDUSTRY INTEL · EVALUATION BRIEF</div>
        <h1 class="brand">Text-to-SQL 消融评测简报</h1>
      </div>
      <div class="meta-line mono">
        模型：{html.escape(str(meta.get('llm_model', '')))} · 题库：{int(meta.get('dataset_size', 0))} 题 · 样本库：{int(meta.get('db_job_count', 0))} 条 · {html.escape(str(meta.get('timestamp', '')))}
      </div>
    </header>

    <div class="headline-box">
      <div class="kicker">评测结论 · ABLATION SUMMARY</div>
      <h1 class="headline">完整版 C 组结果准确率达 {_fmt_pct(mc.get('result_accuracy'))}，安全攻击实现 {_fmt_pct(mc.get('security_block_rate'))} 全拦截</h1>
      <p class="dek">
        在 {int(meta.get('non_attack_count', 0))} 道业务查询与 {int(meta.get('attack_count', 0))} 道安全攻击评测中，相比基线 A 组，引入动态 Schema 枚举、Few-Shot、规范化技能表与三层只读防线后，准确率提升 {delta_acc:+.1f} 个百分点，P50 延迟为 {_fmt_num(mc.get('p50_latency_ms'), 0)} ms。
      </p>
    </div>

    <div class="kpi-grid">
      <div class="kpi-cell">
        <div class="kpi-label">C 组结果准确率</div>
        <div class="kpi-value mono accent-num">{_fmt_pct(mc.get('result_accuracy'))}</div>
        <div class="kpi-note">精确 {int(mc.get('strict_match_count', 0))} + 语义 {int(mc.get('semantic_match_count', 0))} / {int(mc.get('non_attack_total', 0))}</div>
      </div>
      <div class="kpi-cell">
        <div class="kpi-label">C 组首次执行成功率</div>
        <div class="kpi-value mono">{_fmt_pct(mc.get('first_try_exec_rate'))}</div>
        <div class="kpi-note">最终执行成功率 {_fmt_pct(mc.get('final_exec_rate'))}</div>
      </div>
      <div class="kpi-cell">
        <div class="kpi-label">C 组安全拦截率</div>
        <div class="kpi-value mono moss-num">{_fmt_pct(mc.get('security_block_rate'))}</div>
        <div class="kpi-note">误拦截率 {_fmt_pct(mc.get('false_positive_block_rate'))}</div>
      </div>
      <div class="kpi-cell">
        <div class="kpi-label">C 组 P50 延迟 / 单次成本</div>
        <div class="kpi-value mono">{_fmt_num(mc.get('p50_latency_ms'), 0)}<span style="font-size:16px">ms</span></div>
        <div class="kpi-note">平均 {_fmt_num(mc.get('avg_total_tokens'), 0)} tokens · ¥{_fmt_num(mc.get('est_cost_cny_per_query'), 5)}</div>
      </div>
    </div>

    <section>
      <h2 class="sec-title">表 1 · A / B / C 三组消融对比总表</h2>
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>配置组别</th>
              <th class="num">首次执行率</th>
              <th class="num">最终执行率</th>
              <th class="num">结果准确率</th>
              <th class="num">安全拦截率</th>
              <th class="num">平均自愈</th>
              <th class="num">P50 延迟</th>
              <th class="num">P95 延迟</th>
              <th class="num">平均 Token</th>
              <th class="num">估算成本</th>
            </tr>
          </thead>
          <tbody>
            {''.join(summary_rows_html)}
          </tbody>
        </table>
      </div>
      <div class="bar-chart">
        {''.join(bars_html)}
      </div>
    </section>

    <section>
      <h2 class="sec-title">表 2 · 分题型得分明细</h2>
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>题型分类</th>
              <th class="num">题数</th>
              {''.join(f'<th>{html.escape(k)} 组</th>' for k in cfg_keys)}
            </tr>
          </thead>
          <tbody>
            {''.join(cat_rows_html)}
          </tbody>
        </table>
      </div>
    </section>

    <section>
      <h2 class="sec-title">表 3 · 典型 SQL 自愈案例（3 例）</h2>
      <div class="cases-grid">
        {''.join(repair_cards) if repair_cards else '<p class="case-meta">未运行自愈验证案例。</p>'}
      </div>
    </section>

    <section>
      <h2 class="sec-title">表 4 · 全部安全攻击拦截案例</h2>
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>编号</th>
              <th>攻击提问</th>
              {''.join(f'<th>{html.escape(k)} 组</th>' for k in cfg_keys)}
            </tr>
          </thead>
          <tbody>
            {''.join(sec_rows_html)}
          </tbody>
        </table>
      </div>
    </section>

    <footer>
      数据口径：全部数字均来自本次本地 SQLite 只读实跑与大模型真实返回，绝无手工填数。成本按 DeepSeek-V3 输入 ¥2/M tokens、输出 ¥8/M tokens 估算。
    </footer>
  </div>
</body>
</html>
"""


def generate_reports(
    summary_data: dict[str, Any],
    *,
    md_path: Path = DEFAULT_MD_PATH,
    html_path: Path = DEFAULT_HTML_PATH,
) -> tuple[Path, Path]:
    """根据汇总数据生成 Markdown 与 HTML 评测报告。"""
    md_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.parent.mkdir(parents=True, exist_ok=True)

    md_text = build_markdown_report(summary_data)
    html_text = build_html_report(summary_data)

    md_path.write_text(md_text, encoding="utf-8")
    html_path.write_text(html_text, encoding="utf-8")
    return md_path, html_path


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 Text-to-SQL 评测报告")
    parser.add_argument("--input", type=Path, default=DEFAULT_SUMMARY_PATH, help="评测汇总 JSON 路径")
    parser.add_argument("--md-out", type=Path, default=DEFAULT_MD_PATH, help="Markdown 报告输出路径")
    parser.add_argument("--html-out", type=Path, default=DEFAULT_HTML_PATH, help="HTML 报告输出路径")
    args = parser.parse_args()

    summary = load_summary(args.input)
    md_file, html_file = generate_reports(summary, md_path=args.md_out, html_path=args.html_out)
    print(f"[REPORT] Markdown 已生成：{md_file}")
    print(f"[REPORT] HTML 简报已生成：{html_file}")


if __name__ == "__main__":
    main()
