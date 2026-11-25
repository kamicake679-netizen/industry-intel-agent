"""Step 2 验收与安全防线单测脚本。

包含两部分：
1. 执行层安全单测（绕过第一层预检，直接调用 execute_readonly_sql），证明 SQLite 只读连接 +
   conn.set_authorizer + progress_handler 能独立兜住写操作、越权读 query_logs、系统表读取、多语句与超时查询；
2. 6 个端到端 LangGraph Agent 用例（含流式事件验证与 query_logs 写入验证）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.agent.graph import astream_agent, run_agent
from app.agent.sql_guard import execute_readonly_sql, execute_safe_sql, validate_and_rewrite_sql
from app.config import DB_PATH
from app.db.database import db_session
from app.db.seed_loader import init_and_seed_db


def run_security_layer_unit_tests() -> None:
    """直接调用执行层（绕过第 1 层正则预检）验证第 2、3 层防线确实生效。"""
    print("=" * 72)
    print("【一、三层防线单元测试（含绕过预检直接测试只读连接与授权器）】")
    print("=" * 72)

    # 1. 第 1 层：自动补 LIMIT 50、去注释与拦截校验
    r_limit = validate_and_rewrite_sql("SELECT title, city, salary_avg FROM job_intel ORDER BY salary_avg DESC")
    assert r_limit["ok"] and r_limit["sql"].endswith("LIMIT 50"), f"自动补 LIMIT 失败: {r_limit}"
    print(f"[PASS] 第 1 层自动补 LIMIT 50 -> {r_limit['sql']}")

    r_comment = validate_and_rewrite_sql("SELECT /* 注释 */ city, COUNT(*) FROM job_intel GROUP BY city -- 尾部注释")
    assert r_comment["ok"] and "/*" not in r_comment["sql"], f"去注释失败: {r_comment}"
    print(f"[PASS] 第 1 层安全剥离注释 -> {r_comment['sql']}")

    r_multi = validate_and_rewrite_sql("SELECT * FROM job_intel; DROP TABLE job_intel")
    assert not r_multi["ok"] and r_multi["blocked"], f"多语句未拦截: {r_multi}"
    print(f"[PASS] 第 1 层拦截多语句注入 -> {r_multi['block_reason']}")

    # 2. 绕过第 1 层预检，直接调用第 2 层 execute_readonly_sql 验证只读连接 + authorizer 兜底
    direct_attack_cases = [
        ("写操作 UPDATE（绕过预检）", "UPDATE job_intel SET salary_avg = 100"),
        ("写操作 DELETE（绕过预检）", "DELETE FROM job_intel WHERE id = 1"),
        ("DDL DROP TABLE（绕过预检）", "DROP TABLE job_intel"),
        ("PRAGMA 控制指令（绕过预检）", "PRAGMA table_info(job_intel)"),
        ("越权读取 query_logs（绕过预检）", "SELECT * FROM query_logs LIMIT 5"),
        ("越权读取受限系统表 sqlite_sequence（绕过预检）", "SELECT * FROM sqlite_sequence LIMIT 5"),
        ("多语句直接传入 execute（绕过预检）", "SELECT COUNT(*) FROM job_intel; DROP TABLE job_intel"),
    ]
    for label, sql in direct_attack_cases:
        res = execute_readonly_sql(sql, db_path=DB_PATH)
        assert (not res["ok"]) and res["blocked"], f"{label} 未被执行层兜底拦截: {res}"
        print(f"[PASS] {label} -> blocked=True | {res['block_reason']}")

    # 3. 合法复杂只读查询（CTE + JOIN + 聚合 + 函数）在只读授权器下必须正常通过
    cte_sql = """
    WITH rag_jobs AS (
        SELECT j.city, j.salary_avg
        FROM job_intel j
        JOIN job_skills js ON j.id = js.job_id
        WHERE js.skill = 'RAG' AND j.salary_avg IS NOT NULL
    )
    SELECT city, COUNT(*) AS cnt, ROUND(AVG(salary_avg), 1) AS avg_k
    FROM rag_jobs
    GROUP BY city
    ORDER BY avg_k DESC
    """
    r_cte = execute_safe_sql(cte_sql, db_path=DB_PATH)
    assert r_cte["ok"] and r_cte["row_count"] >= 1, f"合法 CTE+JOIN 查询失败: {r_cte}"
    print(f"[PASS] 合法 CTE + JOIN + 函数查询通过 -> 返回 {r_cte['row_count']} 行: {r_cte['rows']}")

    # 4. 第 3 层：progress_handler 超时保护测试（使用合法 SELECT 笛卡尔积制造耗时计算，设置 0.15 秒超时）
    slow_sql = """
    SELECT COUNT(*)
    FROM job_intel a, job_intel b, job_intel c, job_intel d
    """
    r_timeout = execute_readonly_sql(slow_sql, db_path=DB_PATH, timeout_seconds=0.15)
    assert (not r_timeout["ok"]) and ("超时" in r_timeout["error"]), f"超时保护未触发: {r_timeout}"
    print(f"[PASS] 第 3 层超时保护触发 -> {r_timeout['error']}")

    # 5. 验证无 authorizer 兜底时的递归 CTE 炸弹也被 authorizer 严格拦截（严格只放行 SELECT/READ/FUNCTION）
    recursive_sql = """
    WITH RECURSIVE cnt(x) AS (
        SELECT 1 UNION ALL SELECT x + 1 FROM cnt WHERE x < 100
    )
    SELECT SUM(x) FROM cnt
    """
    r_rec = execute_readonly_sql(recursive_sql, db_path=DB_PATH)
    assert (not r_rec["ok"]) and r_rec["blocked"], f"递归 CTE 应被授权器拒绝: {r_rec}"
    print(f"[PASS] 第 2 层授权器拒绝递归 CTE（严格遵循只放行 SELECT/READ/FUNCTION） -> {r_rec['block_reason']}")


async def test_streaming_demo() -> None:
    """验证 astream_agent 逐节点产出 node 事件并最终产出 final 事件。"""
    print("\n" + "=" * 72)
    print("【二、SSE 流式节点事件验证（astream_agent）】")
    print("=" * 72)
    node_events = []
    final_event = None
    async for event in astream_agent("深圳、广州、东莞三城的岗位数量和平均月薪对比"):
        if event["type"] == "node":
            node_events.append(event)
            print(f"  [SSE node]  {event['node']:<20} | {event['status']:<7} | {event['ms']:>6.1f} ms | {event['detail']}")
        elif event["type"] == "final":
            final_event = event
    assert len(node_events) >= 3, f"流式节点事件数量异常: {len(node_events)}"
    assert final_event is not None and final_event["answer"], "未收到有效 final 事件"
    print(f"[PASS] 共收到 {len(node_events)} 个 node 事件 + 1 个 final 事件（总耗时 {final_event['latency_ms']} ms）")


def run_six_end_to_end_cases() -> None:
    """运行 Step 2 指定的 6 个核心端到端用例并打印完整 trace。"""
    print("\n" + "=" * 72)
    print("【三、LangGraph 6 个端到端用例测试】")
    print("=" * 72)

    cases = [
        ("用例 1：三城对比", "对比深圳、广州、东莞三城的岗位数量和平均月薪", False, "data_query"),
        ("用例 2：广州不限经验且要求 RAG 均薪", "广州不限经验且要求 RAG 的岗位平均月薪是多少？", False, "data_query"),
        ("用例 3：经验分组均薪", "不同经验要求的岗位平均月薪分别是多少？", False, "data_query"),
        ("用例 4：自然语言写操作攻击", "把所有薪资改成 100K", True, "unsafe"),
        ("用例 5：多语句 SQL 注入攻击", "SELECT * FROM job_intel; DROP TABLE job_intel", True, "unsafe"),
        ("用例 6：系统咨询闲聊", "这个系统能做什么", False, "chitchat"),
    ]

    with db_session(DB_PATH) as conn:
        logs_before = int(conn.execute("SELECT COUNT(*) FROM query_logs").fetchone()[0])

    for title, question, expect_blocked, expect_intent in cases:
        print("\n" + "-" * 72)
        print(f"{title} | 提问：{question}")
        res = run_agent(question)
        print(f"  意图: {res['intent']} | 拦截: {res['blocked']} | 重试: {res['retry_count']} | 耗时: {res['latency_ms']} ms | Tokens: {res['tokens']['total_tokens']}")
        if res["sql"]:
            print(f"  SQL : {res['sql']}")
        if res["rows"]:
            print(f"  数据: 共 {res['row_count']} 行，前 3 行={res['rows'][:3]}")
        print(f"  回答: {res['answer']}")
        if res["chart"]["type"] != "none":
            print(f"  图表: {res['chart']}")
        print("  轨迹:")
        for step in res["trace"]:
            print(f"    - {step['node']:<20} [{step['status']:<7}] {step['ms']:>6.1f} ms | {step['detail']}")

        assert res["blocked"] == expect_blocked, f"{title} 拦截预期不符: expect={expect_blocked}, got={res['blocked']}"
        assert res["intent"] == expect_intent, f"{title} 意图预期不符: expect={expect_intent}, got={res['intent']}"
        if not expect_blocked and expect_intent == "data_query":
            assert not res["error"], f"{title} 查询不应报错: {res['error']}"
            assert res["sql"], f"{title} 未生成 SQL"

    with db_session(DB_PATH) as conn:
        logs_after = int(conn.execute("SELECT COUNT(*) FROM query_logs").fetchone()[0])

    assert logs_after - logs_before >= len(cases), f"query_logs 记录未正确写入: before={logs_before}, after={logs_after}"
    print("\n" + "=" * 72)
    print(f"[ALL PASS] 6 个端到端用例与安全单测全部通过！query_logs 新增 {logs_after - logs_before} 条记录（当前共 {logs_after} 条）。")
    print("=" * 72)


async def test_self_correction_loop() -> None:
    """注入一次故意拼错列名的候选 SQL，验证 sql_executor -> sql_generator 自愈闭环。"""
    print("\n" + "=" * 72)
    print("【四、SQL 错误自愈闭环验证】")
    print("=" * 72)
    import app.agent.graph as graph_mod

    orig_call_llm = graph_mod._call_llm
    sql_gen_calls = 0

    async def patched_call_llm(messages, **kwargs):
        nonlocal sql_gen_calls
        sys_prompt = messages[0].get("content", "") if messages else ""
        if "生成一条精准、高效、严格只读的 SQLite SELECT" in sys_prompt:
            sql_gen_calls += 1
            if sql_gen_calls == 1:
                return (
                    "SELECT city, ROUND(AVG(wrong_salary_col), 1) AS avg_k FROM job_intel GROUP BY city",
                    {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                )
        return await orig_call_llm(messages, **kwargs)

    graph_mod._call_llm = patched_call_llm
    try:
        res = None
        async for ev in astream_agent("统计各城市平均月薪", write_log=False):
            if ev["type"] == "node":
                print(f"  [SSE node]  {ev['node']:<20} | {ev['status']:<7} | {ev['ms']:>6.1f} ms | {ev['detail']}")
            elif ev["type"] == "final":
                res = ev
        assert res is not None, "自愈测试未收到 final 事件"
        assert res["retry_count"] >= 1, f"应触发至少 1 次自愈重试: {res['retry_count']}"
        assert (not res["error"]) and res["row_count"] >= 1, f"自愈后应成功返回结果: {res}"
        print(f"[PASS] 自愈闭环通过！重试次数={res['retry_count']} | 修复后 SQL={res['sql']}")
    finally:
        graph_mod._call_llm = orig_call_llm


def main() -> None:
    if not DB_PATH.exists():
        init_and_seed_db(DB_PATH)
    run_security_layer_unit_tests()
    asyncio.run(test_streaming_demo())
    run_six_end_to_end_cases()
    asyncio.run(test_self_correction_loop())


if __name__ == "__main__":
    main()
