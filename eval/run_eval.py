"""Step 7 Text-to-SQL 自动化消融评测流水线（A / B / C 三组对比）。

用法示例：
    python eval/run_eval.py --limit 6 --config all
    python eval/run_eval.py --config all --concurrency 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.agent.graph import (
    _arun_agent,
    _call_llm,
    _clean_generated_sql,
    _extract_json_object,
    sql_executor_node,
    sql_generator_node,
)
from app.agent.prompts import MINIMAL_SCHEMA_DESCRIPTION, build_sql_generator_messages
from app.agent.sql_guard import _build_readonly_uri, execute_safe_sql
from app.config import DB_PATH, settings
from app.db.database import get_schema_description
from app.db.seed_loader import init_and_seed_db
from eval.report_generator import generate_reports

DEFAULT_DATASET_PATH = ROOT_DIR / "data" / "eval_sql_dataset.json"
RESULTS_DIR = ROOT_DIR / "eval" / "results"

# A / B 基线组使用的弱正则黑名单（仅拦截显式写/DDL语句头，未去注释、未拦 query_logs、无授权器）
_WEAK_BASELINE_REGEX = re.compile(
    r"^\s*(?:INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|DETACH|PRAGMA)\b",
    re.IGNORECASE,
)

# DeepSeek-V3 参考单价（元 / 1M tokens）：输入 ¥2，输出 ¥8
_INPUT_PRICE_PER_M = 2.0
_OUTPUT_PRICE_PER_M = 8.0


def select_dataset_items(dataset: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """按题型均衡抽样（当 limit > 0 且小于总数时保证各类别均匀覆盖）。"""
    if limit <= 0 or limit >= len(dataset):
        return list(dataset)

    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in dataset:
        buckets.setdefault(str(item.get("category") or "other"), []).append(item)

    selected: list[dict[str, Any]] = []
    order = ["basic_filter", "group_agg", "skill_complex", "security_attack"]
    for cat in buckets:
        if cat not in order:
            order.append(cat)

    idx = 0
    while len(selected) < limit:
        added_in_round = False
        for cat in order:
            items = buckets.get(cat) or []
            if idx < len(items):
                selected.append(items[idx])
                added_in_round = True
                if len(selected) >= limit:
                    break
        if not added_in_round:
            break
        idx += 1

    return selected


async def _call_llm_retry(
    messages: list[dict[str, str]],
    *,
    json_mode: bool = False,
    temperature: float = 0.0,
    max_tokens: int = 500,
    max_attempts: int = 3,
) -> tuple[str, dict[str, int]]:
    """带指数退避的异步 LLM 调用，应对并发 429 或瞬时网络抖动。"""
    delay = 1.5
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await _call_llm(
                messages,
                json_mode=json_mode,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(delay)
                delay *= 2.0
    raise last_exc  # type: ignore[misc]


def _normalize_cell(val: Any) -> tuple[str, Any]:
    """将单元格规范化为 (类型标记, 值)，便于忽略列名比较。"""
    if val is None:
        return ("null", None)
    if isinstance(val, bool):
        return ("num", float(int(val)))
    if isinstance(val, (int, float)):
        return ("num", float(val))
    s = str(val).strip()
    try:
        return ("num", float(s))
    except ValueError:
        return ("str", s)


def _cells_equal(c1: tuple[str, Any], c2: tuple[str, Any], tol: float = 0.01) -> bool:
    if c1[0] != c2[0]:
        return False
    if c1[0] == "null":
        return True
    if c1[0] == "num":
        if math.isnan(c1[1]) or math.isnan(c2[1]):
            return False
        return abs(float(c1[1]) - float(c2[1])) <= tol
    return c1[1] == c2[1]


def _rows_match(row_a: list[tuple[str, Any]], row_b: list[tuple[str, Any]], tol: float = 0.01) -> bool:
    """在忽略列名与列序的前提下比较两行（支持数值容差）。"""
    if len(row_a) != len(row_b):
        return False
    used = [False] * len(row_b)
    for ca in row_a:
        found = False
        for j, cb in enumerate(row_b):
            if not used[j] and _cells_equal(ca, cb, tol=tol):
                used[j] = True
                found = True
                break
        if not found:
            return False
    return True


def compare_result_sets(
    ref_rows: list[dict[str, Any]],
    pred_rows: list[dict[str, Any]],
    *,
    tol: float = 0.01,
) -> bool:
    """严格比对参考结果集与预测结果集（忽略列名与行序，数值容差 0.01）。"""
    if len(ref_rows) != len(pred_rows):
        return False
    if not ref_rows:
        return True

    norm_ref = [[_normalize_cell(v) for v in row.values()] for row in ref_rows]
    norm_pred = [[_normalize_cell(v) for v in row.values()] for row in pred_rows]

    if len(norm_ref[0]) != len(norm_pred[0]):
        return False

    used = [False] * len(norm_pred)
    for r_row in norm_ref:
        matched = False
        for j, p_row in enumerate(norm_pred):
            if not used[j] and _rows_match(r_row, p_row, tol=tol):
                used[j] = True
                matched = True
                break
        if not matched:
            return False
    return True


async def judge_semantic_equivalence(
    *,
    question: str,
    reference_sql: str,
    ref_rows: list[dict[str, Any]],
    pred_sql: str,
    pred_rows: list[dict[str, Any]],
) -> tuple[bool, str]:
    """当严格结果集不一致时，调用 LLM 判定预测 SQL 与结果是否在语义上等价。"""
    system_prompt = (
        "你是严谨的 SQL 评测裁判。请判断【候选查询】的 SQL 与执行结果是否准确回答了【用户问题】，"
        "并与【参考查询】在业务口径上语义等价。\n"
        "判定为等价的情况：核心指标数值一致（允许四舍五入差异、额外附带合理的辅助列如岗位数/城市名、不同的列命名或排序方式）。\n"
        "判定为不等价的情况：过滤条件错误、遗漏关键筛选（如城市/经验/技能）、聚合维度错误、关键数值明显不符或返回空结果而参考有结果。\n"
        '严格返回 JSON：{"equivalent": true 或 false, "reason": "不超过40字的理由"}'
    )
    user_msg = (
        f"用户问题：{question}\n\n"
        f"参考 SQL：{reference_sql}\n"
        f"参考结果（共 {len(ref_rows)} 行，展示前 12 行）：{json.dumps(ref_rows[:12], ensure_ascii=False)}\n\n"
        f"候选 SQL：{pred_sql}\n"
        f"候选结果（共 {len(pred_rows)} 行，展示前 12 行）：{json.dumps(pred_rows[:12], ensure_ascii=False)}"
    )
    try:
        content, _ = await _call_llm_retry(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            json_mode=True,
            temperature=0.0,
            max_tokens=180,
        )
        parsed = _extract_json_object(content)
        return bool(parsed.get("equivalent", False)), str(parsed.get("reason") or "")
    except Exception as exc:
        return False, f"裁判调用失败：{exc}"


def _execute_baseline_sql(sql: str, db_path: Path = DB_PATH) -> dict[str, Any]:
    """A / B 基线组执行器：仅做弱正则黑名单检查 + 只读连接（无注释剥离/表白名单/authorizer）。"""
    cleaned = (sql or "").strip()
    if not cleaned:
        return {
            "ok": False,
            "sql": "",
            "columns": [],
            "rows": [],
            "row_count": 0,
            "error": "SQL 为空",
            "blocked": True,
            "block_reason": "空 SQL",
        }

    if _WEAK_BASELINE_REGEX.match(cleaned):
        return {
            "ok": False,
            "sql": cleaned,
            "columns": [],
            "rows": [],
            "row_count": 0,
            "error": "命中基线弱正则写操作拦截",
            "blocked": True,
            "block_reason": "基线正则拦截写操作语句头",
        }

    start_ts = time.monotonic()
    timed_out = False

    def _eval_safety_timeout() -> int:
        nonlocal timed_out
        if time.monotonic() - start_ts > 3.0:
            timed_out = True
            return 1
        return 0

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(_build_readonly_uri(db_path), uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.set_progress_handler(_eval_safety_timeout, 2000)
        cur = conn.execute(cleaned)
        columns = [str(col[0]) for col in (cur.description or [])]
        fetched = cur.fetchmany(200)
        rows = [{col: row[col] for col in columns} for row in fetched]
        return {
            "ok": True,
            "sql": cleaned,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "error": "",
            "blocked": False,
            "block_reason": "",
        }
    except Exception as exc:
        err = "评测超时（缺少第三层护栏）" if timed_out else str(exc)
        return {
            "ok": False,
            "sql": cleaned,
            "columns": [],
            "rows": [],
            "row_count": 0,
            "error": err,
            "blocked": False,
            "block_reason": "",
        }
    finally:
        if conn is not None:
            try:
                conn.set_progress_handler(None, 0)
            except Exception:
                pass
            conn.close()


def _build_minimal_baseline_messages(question: str) -> list[dict[str, str]]:
    """Config A 基线极简 Prompt（无枚举、无 few-shot、无技能 JOIN 提示）。"""
    system_prompt = (
        "你是 SQLite SQL 生成助手。请根据表结构直接输出一条 SQLite 查询语句（不要输出解释或 Markdown 代码块）。\n"
        f"{MINIMAL_SCHEMA_DESCRIPTION}"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question.strip()},
    ]


async def _run_single_case(
    cfg: str,
    item: dict[str, Any],
    *,
    full_schema_desc: str,
    skip_llm_judge: bool,
) -> dict[str, Any]:
    """执行单题单配置评测并完成结果比对。"""
    qid = str(item["id"])
    category = str(item["category"])
    question = str(item["question"])
    reference_sql = str(item.get("reference_sql") or "")
    expect_blocked = bool(item.get("expect_blocked", False))

    t0 = time.perf_counter()
    sql = ""
    ok = False
    blocked = False
    block_reason = ""
    error = ""
    retry_count = 0
    rows: list[dict[str, Any]] = []
    row_count = 0
    tokens = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    repair_first_error = ""

    if cfg in {"A", "B"}:
        if cfg == "A":
            messages = _build_minimal_baseline_messages(question)
        else:
            messages = build_sql_generator_messages(
                question=question,
                schema_description=full_schema_desc,
                history=None,
                use_few_shot=True,
            )
        raw_sql, tokens = await _call_llm_retry(messages, json_mode=False, temperature=0.0, max_tokens=500)
        sql = _clean_generated_sql(raw_sql)
        exec_res = _execute_baseline_sql(sql, db_path=DB_PATH)
        ok = bool(exec_res["ok"])
        blocked = bool(exec_res["blocked"])
        block_reason = str(exec_res["block_reason"] or "")
        error = str(exec_res["error"] or "")
        rows = list(exec_res["rows"] or [])
        row_count = int(exec_res["row_count"] or 0)
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
    else:
        res = await _arun_agent(
            question=question,
            history=None,
            self_correct=True,
            max_retries=3,
            schema_mode="full",
            use_few_shot=True,
            enable_layer1=True,
            enable_readonly_authorizer=True,
            db_path=DB_PATH,
            write_log=False,
        )
        sql = str(res.get("sql") or "")
        blocked = bool(res.get("blocked", False))
        block_reason = str(res.get("block_reason") or "")
        error = str(res.get("error") or "")
        ok = (not blocked) and (not error)
        retry_count = int(res.get("retry_count") or 0)
        rows = list(res.get("rows") or [])
        row_count = int(res.get("row_count") or 0)
        tokens = dict(res.get("tokens") or tokens)
        latency_ms = float(res.get("latency_ms") or round((time.perf_counter() - t0) * 1000, 1))
        for tr in res.get("trace") or []:
            if tr.get("node") == "sql_executor" and tr.get("status") == "retry" and not repair_first_error:
                repair_first_error = str(tr.get("detail") or "")

    strict_match = False
    semantic_match = False
    judge_reason = ""

    if not expect_blocked and ok and reference_sql:
        ref_res = execute_safe_sql(reference_sql, db_path=DB_PATH)
        ref_rows = list(ref_res.get("rows") or [])
        strict_match = compare_result_sets(ref_rows, rows, tol=0.01)
        if not strict_match and not skip_llm_judge:
            semantic_match, judge_reason = await judge_semantic_equivalence(
                question=question,
                reference_sql=reference_sql,
                ref_rows=ref_rows,
                pred_sql=sql,
                pred_rows=rows,
            )

    return {
        "id": qid,
        "category": category,
        "question": question,
        "expect_blocked": expect_blocked,
        "sql": sql,
        "ok": ok,
        "blocked": blocked,
        "block_reason": block_reason,
        "error": error,
        "retry_count": retry_count,
        "repair_first_error": repair_first_error,
        "row_count": row_count,
        "strict_match": strict_match,
        "semantic_match": semantic_match,
        "accurate": strict_match or semantic_match,
        "judge_reason": judge_reason,
        "latency_ms": latency_ms,
        "tokens": tokens,
    }


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(ordered[int(k)])
    return float(ordered[f] * (c - k) + ordered[c] * (k - f))


def compute_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总单组配置的全部评测指标。"""
    non_atk = [r for r in results if not r["expect_blocked"]]
    atk = [r for r in results if r["expect_blocked"]]

    non_total = len(non_atk)
    atk_total = len(atk)

    first_ok = sum(1 for r in non_atk if r["ok"] and int(r["retry_count"]) == 0)
    final_ok = sum(1 for r in non_atk if r["ok"])
    strict_cnt = sum(1 for r in non_atk if r["strict_match"])
    sem_cnt = sum(1 for r in non_atk if (not r["strict_match"]) and r["semantic_match"])
    acc_cnt = strict_cnt + sem_cnt
    fp_blocked = sum(1 for r in non_atk if r["blocked"])
    atk_blocked = sum(1 for r in atk if r["blocked"])

    latencies = [float(r["latency_ms"]) for r in results]
    prompt_toks = [int((r.get("tokens") or {}).get("prompt_tokens", 0)) for r in results]
    comp_toks = [int((r.get("tokens") or {}).get("completion_tokens", 0)) for r in results]
    total_toks = [int((r.get("tokens") or {}).get("total_tokens", 0)) for r in results]

    avg_prompt = sum(prompt_toks) / len(results) if results else 0.0
    avg_comp = sum(comp_toks) / len(results) if results else 0.0
    avg_total = sum(total_toks) / len(results) if results else 0.0
    est_cost = (avg_prompt * _INPUT_PRICE_PER_M + avg_comp * _OUTPUT_PRICE_PER_M) / 1_000_000.0

    by_category: dict[str, dict[str, Any]] = {}
    for cat in ("basic_filter", "group_agg", "skill_complex", "security_attack"):
        items = [r for r in results if r["category"] == cat]
        if not items:
            continue
        if cat == "security_attack":
            b_cnt = sum(1 for r in items if r["blocked"])
            by_category[cat] = {
                "total": len(items),
                "blocked_count": b_cnt,
                "block_rate": round(b_cnt * 100.0 / len(items), 1),
            }
        else:
            ok_cnt = sum(1 for r in items if r["ok"])
            ac_cnt = sum(1 for r in items if r["accurate"])
            by_category[cat] = {
                "total": len(items),
                "final_exec_rate": round(ok_cnt * 100.0 / len(items), 1),
                "result_accuracy": round(ac_cnt * 100.0 / len(items), 1),
            }

    return {
        "total": len(results),
        "non_attack_total": non_total,
        "attack_total": atk_total,
        "first_try_exec_rate": round(first_ok * 100.0 / non_total, 1) if non_total else 0.0,
        "final_exec_rate": round(final_ok * 100.0 / non_total, 1) if non_total else 0.0,
        "strict_match_count": strict_cnt,
        "semantic_match_count": sem_cnt,
        "strict_accuracy": round(strict_cnt * 100.0 / non_total, 1) if non_total else 0.0,
        "result_accuracy": round(acc_cnt * 100.0 / non_total, 1) if non_total else 0.0,
        "security_block_rate": round(atk_blocked * 100.0 / atk_total, 1) if atk_total else 0.0,
        "false_positive_block_rate": round(fp_blocked * 100.0 / non_total, 1) if non_total else 0.0,
        "avg_retries": round(sum(int(r["retry_count"]) for r in non_atk) / non_total, 2) if non_total else 0.0,
        "p50_latency_ms": round(_percentile(latencies, 0.50), 1),
        "p95_latency_ms": round(_percentile(latencies, 0.95), 1),
        "avg_prompt_tokens": round(avg_prompt, 1),
        "avg_completion_tokens": round(avg_comp, 1),
        "avg_total_tokens": round(avg_total, 1),
        "est_cost_cny_per_query": round(est_cost, 6),
        "by_category": by_category,
    }


async def run_repair_probes(needed: int = 3) -> list[dict[str, Any]]:
    """当主评测自然自愈案例不足 3 个时，运行真实故障注入自愈闭环验证。"""
    if needed <= 0:
        return []

    seeds = [
        {
            "probe_id": "P1",
            "fault_type": "误用不存在列 skills（未 JOIN job_skills）",
            "question": "统计要求 RAG 技能的岗位在深圳、广州、东莞三城的岗位数量与平均月薪",
            "bad_sql": "SELECT city, COUNT(*) AS job_count, ROUND(AVG(salary_avg), 1) AS avg_salary_k FROM job_intel WHERE skills LIKE '%RAG%' GROUP BY city",
        },
        {
            "probe_id": "P2",
            "fault_type": "错误表名 jobs（应为 job_intel）",
            "question": "广州有多少个应届生友好岗位？它们的平均月薪是多少？",
            "bad_sql": "SELECT COUNT(*) AS job_count, ROUND(AVG(salary_avg), 1) AS avg_salary_k FROM jobs WHERE city = '广州' AND fresh_grad_friendly = 1",
        },
        {
            "probe_id": "P3",
            "fault_type": "SQL 语法错误（SELECT 缺少逗号）",
            "question": "按学历要求分组统计岗位数量和平均月薪",
            "bad_sql": "SELECT education COUNT(*) AS job_count, ROUND(AVG(salary_avg), 1) AS avg_salary_k FROM job_intel GROUP BY education",
        },
    ]

    probes: list[dict[str, Any]] = []
    for seed in seeds[:needed]:
        t0 = time.perf_counter()
        state: dict[str, Any] = {
            "question": seed["question"],
            "history": [],
            "sql": seed["bad_sql"],
            "error": "",
            "retry_count": 0,
            "max_retries": 3,
            "self_correct": True,
            "schema_mode": "full",
            "use_few_shot": True,
            "enable_layer1": True,
            "enable_readonly_authorizer": True,
            "db_path": str(DB_PATH),
        }
        exec1 = await sql_executor_node(state)  # type: ignore[arg-type]
        initial_error = str(exec1.get("error") or "")
        state.update({k: v for k, v in exec1.items() if k != "trace"})

        gen_fix = await sql_generator_node(state)  # type: ignore[arg-type]
        repaired_sql = str(gen_fix.get("sql") or "")
        state.update({k: v for k, v in gen_fix.items() if k != "trace"})

        exec2 = await sql_executor_node(state)  # type: ignore[arg-type]
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        probes.append(
            {
                "probe_id": seed["probe_id"],
                "fault_type": seed["fault_type"],
                "question": seed["question"],
                "initial_sql": seed["bad_sql"],
                "initial_error": initial_error,
                "repaired_sql": repaired_sql,
                "ok": bool(exec2.get("row_count", 0) >= 0 and not exec2.get("error") and not exec2.get("blocked")),
                "row_count": int(exec2.get("row_count") or 0),
                "latency_ms": latency_ms,
            }
        )
    return probes


async def run_evaluation(args: argparse.Namespace) -> dict[str, Any]:
    """执行消融评测主流程并生成 JSON 与双格式报告。"""
    init_and_seed_db(DB_PATH)
    with args.dataset.open("r", encoding="utf-8") as f:
        full_dataset: list[dict[str, Any]] = json.load(f)

    items = select_dataset_items(full_dataset, args.limit)
    cfg_list = ["A", "B", "C"] if args.config == "all" else [args.config.upper()]
    full_schema_desc = get_schema_description(DB_PATH)

    conn = sqlite3.connect(_build_readonly_uri(DB_PATH), uri=True)
    db_job_count = int(conn.execute("SELECT COUNT(*) FROM job_intel").fetchone()[0])
    conn.close()

    cat_counter = Counter(str(it["category"]) for it in items)
    print("=" * 72)
    print(f"Text-to-SQL 消融评测启动 | 模型: {settings.llm_model}")
    print(f"样本数: {len(items)} 题 {dict(cat_counter)} | 配置: {cfg_list} | 并发: {args.concurrency}")
    print("=" * 72)

    sem = asyncio.Semaphore(max(1, args.concurrency))
    configs_output: dict[str, Any] = {}

    for cfg in cfg_list:
        print(f"\n>>> 开始评测配置 {cfg} 组（共 {len(items)} 题）...")

        async def _worker(it: dict[str, Any]) -> dict[str, Any]:
            async with sem:
                res = await _run_single_case(
                    cfg,
                    it,
                    full_schema_desc=full_schema_desc,
                    skip_llm_judge=args.skip_llm_judge,
                )
                tag = (
                    "BLOCKED"
                    if res["blocked"]
                    else ("OK-STRICT" if res["strict_match"] else ("OK-SEM" if res["semantic_match"] else ("OK-DIFF" if res["ok"] else "ERR")))
                )
                print(
                    f"  [{cfg}] {res['id']} ({res['category']:<15}) | {tag:<9} | "
                    f"{res['latency_ms']:>6.0f}ms | {res['question'][:28]}"
                )
                return res

        results = await asyncio.gather(*(_worker(it) for it in items))
        results.sort(key=lambda r: r["id"])
        metrics = compute_metrics(results)
        configs_output[cfg] = {"metrics": metrics, "results": results}
        print(
            f"<<< {cfg} 组完成 | 首次执行={metrics['first_try_exec_rate']}% | "
            f"最终执行={metrics['final_exec_rate']}% | 准确率={metrics['result_accuracy']}% | "
            f"安全拦截={metrics['security_block_rate']}% | P50={metrics['p50_latency_ms']}ms"
        )

    repair_probes: list[dict[str, Any]] = []
    if "C" in configs_output and not args.skip_repair_probe:
        natural_cnt = sum(
            1
            for r in configs_output["C"]["results"]
            if not r["expect_blocked"] and int(r["retry_count"]) > 0 and r["ok"]
        )
        if natural_cnt < 3:
            print(f"\n>>> 主评测 C 组自然自愈案例 {natural_cnt} 个，补充运行 {3 - natural_cnt} 个自愈专项验证...")
            repair_probes = await run_repair_probes(3 - natural_cnt)
            for p in repair_probes:
                print(f"  [PROBE {p['probe_id']}] ok={p['ok']} rows={p['row_count']} | {p['fault_type']}")

    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_data: dict[str, Any] = {
        "meta": {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "llm_model": settings.llm_model,
            "dataset_path": str(args.dataset.relative_to(ROOT_DIR)),
            "dataset_size": len(items),
            "non_attack_count": sum(1 for it in items if not it["expect_blocked"]),
            "attack_count": sum(1 for it in items if it["expect_blocked"]),
            "db_job_count": db_job_count,
            "configs": cfg_list,
        },
        "configs": configs_output,
        "repair_probe_cases": repair_probes,
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts_path = RESULTS_DIR / f"eval_{ts_str}.json"
    latest_path = RESULTS_DIR / "latest_summary.json"
    ts_path.write_text(json.dumps(summary_data, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_path.write_text(json.dumps(summary_data, ensure_ascii=False, indent=2), encoding="utf-8")

    md_path, html_path = generate_reports(summary_data)
    print("\n" + "=" * 72)
    print(f"[DONE] 评测结果 JSON：{ts_path}")
    print(f"[DONE] 最新汇总 JSON：{latest_path}")
    print(f"[DONE] Markdown 报告：{md_path}")
    print(f"[DONE] HTML 简报报告：{html_path}")
    print("=" * 72)
    return summary_data


def main() -> None:
    parser = argparse.ArgumentParser(description="Text-to-SQL 自动化消融评测脚本")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET_PATH, help="评测数据集路径")
    parser.add_argument("--config", choices=["A", "B", "C", "all"], default="all", help="评测配置组")
    parser.add_argument("--limit", type=int, default=0, help="抽样题数（0 表示全量）")
    parser.add_argument("--concurrency", type=int, default=3, help="并发数")
    parser.add_argument("--skip-llm-judge", action="store_true", help="跳过 LLM 语义等价裁判")
    parser.add_argument("--skip-repair-probe", action="store_true", help="跳过自愈专项验证")
    args = parser.parse_args()
    asyncio.run(run_evaluation(args))


if __name__ == "__main__":
    main()
