"""LangGraph Text-to-SQL Agent 状态图与流式执行引擎。

拓扑结构：
    START -> intent_router
    intent_router -> chitchat_answer -> END
                  -> security_fallback -> END
                  -> sql_generator -> sql_executor
    sql_executor  -> security_fallback -> END       (blocked)
                  -> sql_generator                  (error 且 retry_count < max_retries 且 self_correct=True)
                  -> error_fallback -> END          (error 超限)
                  -> insight_synthesizer -> END     (ok)
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph
from openai import AsyncOpenAI

from app.agent.prompts import (
    MINIMAL_SCHEMA_DESCRIPTION,
    build_chitchat_messages,
    build_insight_messages,
    build_intent_messages,
    build_sql_generator_messages,
    build_sql_repair_messages,
)
from app.agent.sql_guard import execute_safe_sql, strip_sql_comments
from app.agent.state import AgentState, ChartSpec, TraceEvent
from app.config import DB_PATH, settings
from app.db.database import db_session, get_schema_description, init_db

_EMPTY_CHART: ChartSpec = {"type": "none", "title": "", "labels": [], "values": []}

# 确定性快速安全拦截规则（在意图路由阶段第一时间拦截显式注入、越权与写指令）
_QUICK_UNSAFE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r";\s*(?:DROP|DELETE|UPDATE|INSERT|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|PRAGMA)\b", re.IGNORECASE),
        "检测到多语句 SQL 注入攻击特征，已拦截。",
    ),
    (
        re.compile(r"\b(?:DROP\s+TABLE|DELETE\s+FROM|TRUNCATE\s+TABLE|ALTER\s+TABLE|UPDATE\s+\w+\s+SET|INSERT\s+INTO|ATTACH\s+DATABASE)\b", re.IGNORECASE),
        "检测到 SQL 写操作或 DDL 指令，本系统严格只读，已拦截。",
    ),
    (
        re.compile(r"\b(?:query_logs|sqlite_schema|sqlite_temp_master|sqlite_temp_schema|sqlite_sequence)\b", re.IGNORECASE),
        "禁止查询内部审计日志表或受限系统对象，已拦截。",
    ),
    (
        re.compile(r"(?:把|将).{0,16}(?:薪资|工资|数据|岗位|表|记录).{0,10}(?:改成|改为|修改成|修改为|更新为|设为|删除|清空|删掉)", re.IGNORECASE),
        "检测到修改或删除数据的写操作意图，本系统仅支持只读情报分析。",
    ),
    (
        re.compile(r"(?:删除|清空|篡改|抹掉|删掉).{0,12}(?:所有|全部)?(?:岗位|数据|表|记录|数据库)", re.IGNORECASE),
        "检测到删除或清空数据的写操作意图，本系统仅支持只读情报分析。",
    ),
    (
        re.compile(r"(?:忽略|无视|忘掉).{0,10}(?:之前|前面|上述|所有).{0,8}(?:规则|限制|指令|约束)", re.IGNORECASE),
        "检测到提示词越权诱导（尝试绕过只读安全约束），已拦截。",
    ),
]


def _get_async_client() -> AsyncOpenAI:
    """获取 OpenAI 兼容异步客户端（参数全部来自配置，无厂商硬编码）。"""
    api_key = settings.siliconflow_api_key.strip()
    if not api_key or api_key == "your_api_key_here":
        raise RuntimeError("未配置有效的 SILICONFLOW_API_KEY，无法调用大模型。")
    return AsyncOpenAI(
        api_key=api_key,
        base_url=settings.siliconflow_base_url,
    )


async def _call_llm(
    messages: list[dict[str, str]],
    *,
    json_mode: bool = False,
    temperature: float = 0.1,
    max_tokens: int = 800,
) -> tuple[str, dict[str, int]]:
    """统一异步 LLM 调用，返回 (文本内容, token 用量字典)。"""
    kwargs: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    async with _get_async_client() as client:
        response = await client.chat.completions.create(**kwargs)
    content = (response.choices[0].message.content or "").strip()
    usage_obj = getattr(response, "usage", None)
    usage = {
        "prompt_tokens": int(getattr(usage_obj, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage_obj, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(usage_obj, "total_tokens", 0) or 0),
    }
    return content, usage


def _extract_json_object(text: str) -> dict[str, Any]:
    """容错解析模型返回的 JSON 对象。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        obj = json.loads(cleaned[start : end + 1])
        if isinstance(obj, dict):
            return obj
    raise ValueError(f"无法解析 JSON 对象：{cleaned[:160]}")


def _clean_generated_sql(text: str) -> str:
    """清理模型生成的 SQL（去掉代码块、多余前缀说明与尾分号）。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    # 若模型误输出 "SQL:" 前缀则去掉
    cleaned = re.sub(r"^(?:SQL|sqlite|查询语句)\s*[:：]\s*", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned


def _quick_unsafe_check(question: str) -> str | None:
    """快速规则安全检测，命中返回拦截原因，未命中返回 None。"""
    q = (question or "").strip()
    if not q:
        return None
    for pattern, reason in _QUICK_UNSAFE_PATTERNS:
        if pattern.search(q):
            return reason
    # 额外检查去除注释后的隐藏注入
    stripped = strip_sql_comments(q)
    if stripped != q:
        for pattern, reason in _QUICK_UNSAFE_PATTERNS:
            if pattern.search(stripped):
                return reason
    return None


def _normalize_chart(raw_chart: Any) -> ChartSpec:
    """校验并规范化模型输出的 chart 结构，确保前端 ECharts 安全消费。"""
    if not isinstance(raw_chart, dict):
        return dict(_EMPTY_CHART)  # type: ignore[return-value]

    ctype = str(raw_chart.get("type") or "none").strip().lower()
    if ctype not in {"bar", "line", "none"}:
        ctype = "none"

    title = str(raw_chart.get("title") or "").strip()
    raw_labels = raw_chart.get("labels") or []
    raw_values = raw_chart.get("values") or []

    if ctype == "none" or not isinstance(raw_labels, list) or not isinstance(raw_values, list):
        return dict(_EMPTY_CHART)  # type: ignore[return-value]

    labels: list[str] = []
    values: list[float | int] = []
    for lbl, val in zip(raw_labels, raw_values):
        try:
            num = float(val)
            num_clean: float | int = int(num) if num.is_integer() else round(num, 2)
            labels.append(str(lbl))
            values.append(num_clean)
        except (TypeError, ValueError):
            continue

    if len(labels) < 2 or len(labels) != len(values):
        return dict(_EMPTY_CHART)  # type: ignore[return-value]

    return {
        "type": ctype,
        "title": title or "数据对比",
        "labels": labels[:12],
        "values": values[:12],
    }


def _get_schema_for_state(state: AgentState) -> str:
    """按配置返回完整动态 Schema 或精简基线 Schema（供 Step 7 消融对比使用）。"""
    if state.get("schema_mode", "full") == "minimal":
        return MINIMAL_SCHEMA_DESCRIPTION
    db_path = Path(state.get("db_path") or DB_PATH)
    return get_schema_description(db_path=db_path)


async def intent_router_node(state: AgentState) -> dict[str, Any]:
    """节点 1：意图识别与安全初审。"""
    t0 = time.perf_counter()
    question = (state.get("question") or "").strip()
    history = state.get("history") or []

    if not question:
        ms = round((time.perf_counter() - t0) * 1000, 1)
        return {
            "intent": "chitchat",
            "blocked": False,
            "trace": [
                TraceEvent(node="intent_router", status="ok", detail="空提问转入引导回复", ms=ms)
            ],
        }

    # 先过快速确定性规则防线
    quick_reason = _quick_unsafe_check(question)
    if quick_reason:
        ms = round((time.perf_counter() - t0) * 1000, 1)
        return {
            "intent": "unsafe",
            "blocked": True,
            "block_reason": quick_reason,
            "trace": [
                TraceEvent(node="intent_router", status="blocked", detail=quick_reason, ms=ms)
            ],
        }

    # 调用 LLM 分类意图
    messages = build_intent_messages(question, history)
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    try:
        content, usage = await _call_llm(messages, json_mode=True, temperature=0.0, max_tokens=200)
        parsed = _extract_json_object(content)
        intent = str(parsed.get("intent") or "data_query").strip().lower()
        reason = str(parsed.get("reason") or "").strip()
        if intent not in {"data_query", "chitchat", "unsafe"}:
            intent = "data_query"
    except Exception:
        # 意图分类异常时默认保守进入 data_query（后端还有三层 SQL 防线兜底）
        intent = "data_query"
        reason = "分类解析回退为数据查询"

    ms = round((time.perf_counter() - t0) * 1000, 1)
    if intent == "unsafe":
        block_reason = reason or "检测到写操作或越权查询意图，已安全拦截。"
        return {
            "intent": "unsafe",
            "blocked": True,
            "block_reason": block_reason,
            "trace": [
                TraceEvent(node="intent_router", status="blocked", detail=block_reason, ms=ms)
            ],
            **usage,
        }

    label = "识别为行业数据查询" if intent == "data_query" else "识别为系统咨询/闲聊"
    detail = f"{label}（{reason}）" if reason else label
    return {
        "intent": intent,
        "blocked": False,
        "block_reason": "",
        "trace": [TraceEvent(node="intent_router", status="ok", detail=detail, ms=ms)],
        **usage,
    }


async def chitchat_answer_node(state: AgentState) -> dict[str, Any]:
    """节点 2a：闲聊与系统能力介绍回答。"""
    t0 = time.perf_counter()
    question = (state.get("question") or "").strip()
    history = state.get("history") or []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    try:
        messages = build_chitchat_messages(question or "这个系统能做什么？", history)
        answer, usage = await _call_llm(messages, json_mode=False, temperature=0.3, max_tokens=400)
    except Exception:
        answer = (
            "本系统是「INDUSTRY INTEL 垂直行业情报分析 Agent」，"
            "收录了大湾区（深圳、广州、东莞）AI 与大模型招聘岗位数据。"
            "您可以直接用自然语言提问，例如查询三城均薪对比、RAG/LangGraph 等热门技能薪资、"
            "经验阶梯分布或应届生友好岗位占比。"
        )

    ms = round((time.perf_counter() - t0) * 1000, 1)
    return {
        "answer": answer,
        "chart": dict(_EMPTY_CHART),
        "trace": [
            TraceEvent(node="chitchat_answer", status="ok", detail="已生成问询台说明回复", ms=ms)
        ],
        **usage,
    }


async def sql_generator_node(state: AgentState) -> dict[str, Any]:
    """节点 2b：Text-to-SQL 首次生成 / 报错自愈重写。"""
    t0 = time.perf_counter()
    question = (state.get("question") or "").strip()
    history = state.get("history") or []
    retry_count = int(state.get("retry_count") or 0)
    prev_sql = str(state.get("sql") or "")
    prev_error = str(state.get("error") or "")
    use_few_shot = bool(state.get("use_few_shot", True))
    schema_desc = _get_schema_for_state(state)

    is_repair = retry_count > 0 and bool(prev_error)
    if is_repair:
        messages = build_sql_repair_messages(
            question=question,
            failed_sql=prev_sql,
            error_message=prev_error,
            schema_description=schema_desc,
        )
    else:
        messages = build_sql_generator_messages(
            question=question,
            schema_description=schema_desc,
            history=history,
            use_few_shot=use_few_shot,
        )

    content, usage = await _call_llm(messages, json_mode=False, temperature=0.0, max_tokens=500)
    sql = _clean_generated_sql(content)
    ms = round((time.perf_counter() - t0) * 1000, 1)

    if is_repair:
        trace_item = TraceEvent(
            node="sql_generator",
            status="retry",
            detail=f"第 {retry_count} 次 SQL 自愈重写完成",
            ms=ms,
        )
    else:
        trace_item = TraceEvent(
            node="sql_generator",
            status="ok",
            detail="已生成候选只读 SQL",
            ms=ms,
        )

    return {
        "sql": sql,
        "error": "",
        "trace": [trace_item],
        **usage,
    }


async def sql_executor_node(state: AgentState) -> dict[str, Any]:
    """节点 3：三层安全防线校验 + 只读 SQLite 执行。"""
    t0 = time.perf_counter()
    sql = str(state.get("sql") or "")
    retry_count = int(state.get("retry_count") or 0)
    max_retries = int(state.get("max_retries") or 3)
    self_correct = bool(state.get("self_correct", True))
    enable_layer1 = bool(state.get("enable_layer1", True))
    enable_readonly_authorizer = bool(state.get("enable_readonly_authorizer", True))
    db_path = Path(state.get("db_path") or DB_PATH)

    exec_res = execute_safe_sql(
        sql,
        db_path=db_path,
        enable_layer1=enable_layer1,
        enable_readonly_authorizer=enable_readonly_authorizer,
    )
    ms = round((time.perf_counter() - t0) * 1000, 1)

    if exec_res["blocked"]:
        reason = exec_res["block_reason"] or "SQL 安全防线拦截了本次查询。"
        return {
            "sql": exec_res["sql"],
            "columns": [],
            "rows": [],
            "row_count": 0,
            "blocked": True,
            "block_reason": reason,
            "error": exec_res["error"],
            "trace": [TraceEvent(node="sql_executor", status="blocked", detail=reason, ms=ms)],
        }

    if exec_res["ok"]:
        row_count = int(exec_res["row_count"])
        return {
            "sql": exec_res["sql"],
            "columns": exec_res["columns"],
            "rows": exec_res["rows"],
            "row_count": row_count,
            "blocked": False,
            "block_reason": "",
            "error": "",
            "trace": [
                TraceEvent(
                    node="sql_executor",
                    status="ok",
                    detail=f"只读执行通过，返回 {row_count} 行数据",
                    ms=ms,
                )
            ],
        }

    # 执行出错：判断是否触发自愈重试
    err_msg = str(exec_res["error"] or "未知 SQLite 执行错误")
    if self_correct and retry_count < max_retries:
        next_retry = retry_count + 1
        return {
            "sql": exec_res["sql"],
            "columns": [],
            "rows": [],
            "row_count": 0,
            "blocked": False,
            "error": err_msg,
            "retry_count": next_retry,
            "trace": [
                TraceEvent(
                    node="sql_executor",
                    status="retry",
                    detail=f"执行报错，触发第 {next_retry}/{max_retries} 次自愈：{err_msg[:90]}",
                    ms=ms,
                )
            ],
        }

    return {
        "sql": exec_res["sql"],
        "columns": [],
        "rows": [],
        "row_count": 0,
        "blocked": False,
        "error": err_msg,
        "trace": [
            TraceEvent(
                node="sql_executor",
                status="error",
                detail=f"SQL 执行失败：{err_msg[:100]}",
                ms=ms,
            )
        ],
    }


async def insight_synthesizer_node(state: AgentState) -> dict[str, Any]:
    """节点 4：基于真实查询结果生成简报结论与图表规格。"""
    t0 = time.perf_counter()
    question = (state.get("question") or "").strip()
    sql = str(state.get("sql") or "")
    columns = list(state.get("columns") or [])
    rows = list(state.get("rows") or [])
    row_count = int(state.get("row_count") or 0)
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    messages = build_insight_messages(
        question=question,
        sql=sql,
        columns=columns,
        rows=rows,
        row_count=row_count,
    )

    try:
        content, usage = await _call_llm(messages, json_mode=True, temperature=0.1, max_tokens=600)
        parsed = _extract_json_object(content)
        answer = str(parsed.get("answer") or "").strip()
        chart = _normalize_chart(parsed.get("chart"))
        if not answer:
            raise ValueError("模型未返回 answer 文本")
    except Exception:
        if row_count == 0:
            answer = "当前数据库中未检索到完全符合条件的岗位记录，建议适当放宽城市、经验或技能筛选条件后再试。"
        else:
            preview = "; ".join(
                ", ".join(f"{k}={v}" for k, v in row.items()) for row in rows[:3]
            )
            answer = f"查询共返回 {row_count} 条结果。前几条关键数据如下：{preview}。"
        chart = dict(_EMPTY_CHART)  # type: ignore[assignment]

    ms = round((time.perf_counter() - t0) * 1000, 1)
    chart_note = f"附 {chart['type']} 迷你图" if chart["type"] != "none" else "纯文本结论"
    return {
        "answer": answer,
        "chart": chart,
        "trace": [
            TraceEvent(
                node="insight_synthesizer",
                status="ok",
                detail=f"洞察总结完成（{chart_note}）",
                ms=ms,
            )
        ],
        **usage,
    }


async def security_fallback_node(state: AgentState) -> dict[str, Any]:
    """节点 5a：安全拦截兜底响应。"""
    t0 = time.perf_counter()
    reason = str(state.get("block_reason") or "请求包含写操作、多语句注入或越权访问风险。")
    answer = (
        f"请求已被只读安全防线拦截：{reason}"
        "本问询台仅支持对行业招聘情报执行只读查询分析，不支持修改、删除数据或访问内部审计与系统表。"
    )
    ms = round((time.perf_counter() - t0) * 1000, 1)
    return {
        "blocked": True,
        "block_reason": reason,
        "answer": answer,
        "chart": dict(_EMPTY_CHART),
        "trace": [
            TraceEvent(node="security_fallback", status="blocked", detail="已返回只读安全拦截说明", ms=ms)
        ],
    }


async def error_fallback_node(state: AgentState) -> dict[str, Any]:
    """节点 5b：SQL 多次自愈仍失败时的优雅降级响应。"""
    t0 = time.perf_counter()
    err = str(state.get("error") or "未知查询错误")
    retry_count = int(state.get("retry_count") or 0)
    answer = (
        f"本次查询在经历 {retry_count} 次自动修复后仍未能成功执行（错误信息：{err}）。"
        "建议换一种更具体的表达方式，例如明确指定城市（深圳/广州/东莞）、经验要求或具体技能名称。"
    )
    ms = round((time.perf_counter() - t0) * 1000, 1)
    return {
        "answer": answer,
        "chart": dict(_EMPTY_CHART),
        "trace": [
            TraceEvent(node="error_fallback", status="error", detail="已触发查询失败降级回复", ms=ms)
        ],
    }


def _route_after_intent(state: AgentState) -> str:
    intent = state.get("intent", "data_query")
    if intent == "unsafe" or state.get("blocked"):
        return "security_fallback"
    if intent == "chitchat":
        return "chitchat_answer"
    return "sql_generator"


def _route_after_executor(state: AgentState) -> str:
    if state.get("blocked"):
        return "security_fallback"
    if state.get("error"):
        self_correct = bool(state.get("self_correct", True))
        retry_count = int(state.get("retry_count") or 0)
        max_retries = int(state.get("max_retries") or 3)
        # sql_executor_node 在可重试时已将 retry_count 递增并记为 status="retry"
        trace = state.get("trace") or []
        last_status = trace[-1]["status"] if trace else ""
        if self_correct and last_status == "retry" and retry_count <= max_retries:
            return "sql_generator"
        return "error_fallback"
    return "insight_synthesizer"


def build_agent_graph():
    """构建并编译 LangGraph StateGraph。"""
    builder: StateGraph = StateGraph(AgentState)
    builder.add_node("intent_router", intent_router_node)
    builder.add_node("chitchat_answer", chitchat_answer_node)
    builder.add_node("sql_generator", sql_generator_node)
    builder.add_node("sql_executor", sql_executor_node)
    builder.add_node("insight_synthesizer", insight_synthesizer_node)
    builder.add_node("security_fallback", security_fallback_node)
    builder.add_node("error_fallback", error_fallback_node)

    builder.add_edge(START, "intent_router")
    builder.add_conditional_edges(
        "intent_router",
        _route_after_intent,
        {
            "chitchat_answer": "chitchat_answer",
            "sql_generator": "sql_generator",
            "security_fallback": "security_fallback",
        },
    )
    builder.add_edge("chitchat_answer", END)
    builder.add_edge("security_fallback", END)
    builder.add_edge("sql_generator", "sql_executor")
    builder.add_conditional_edges(
        "sql_executor",
        _route_after_executor,
        {
            "security_fallback": "security_fallback",
            "sql_generator": "sql_generator",
            "error_fallback": "error_fallback",
            "insight_synthesizer": "insight_synthesizer",
        },
    )
    builder.add_edge("error_fallback", END)
    builder.add_edge("insight_synthesizer", END)

    return builder.compile()


agent_graph = build_agent_graph()


def _log_query(
    *,
    db_path: Path,
    question: str,
    sql: str,
    ok: bool,
    retry_count: int,
    blocked: bool,
    latency_ms: float,
) -> None:
    """将每次问询运行记录持久化至 query_logs（独立写入连接，容错不中断主流程）。"""
    try:
        init_db(db_path)
        with db_session(db_path) as conn:
            conn.execute(
                """
                INSERT INTO query_logs (question, sql, ok, retry_count, blocked, latency_ms)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    question,
                    sql,
                    1 if ok else 0,
                    int(retry_count),
                    1 if blocked else 0,
                    round(float(latency_ms), 1),
                ),
            )
            conn.commit()
    except Exception:
        pass


async def astream_agent(
    question: str,
    history: list[dict[str, str]] | None = None,
    *,
    self_correct: bool = True,
    max_retries: int = 3,
    schema_mode: str = "full",
    use_few_shot: bool = True,
    enable_layer1: bool = True,
    enable_readonly_authorizer: bool = True,
    db_path: Path = DB_PATH,
    write_log: bool = True,
) -> AsyncIterator[dict[str, Any]]:
    """基于 LangGraph ``astream(stream_mode="updates")`` 逐节点流式产出事件，最后 yield ``final`` 事件。"""
    t_start = time.perf_counter()

    initial_state: AgentState = {
        "question": question,
        "history": list(history[-6:]) if history else [],
        "intent": "",
        "sql": "",
        "columns": [],
        "rows": [],
        "row_count": 0,
        "error": "",
        "retry_count": 0,
        "max_retries": max_retries,
        "blocked": False,
        "block_reason": "",
        "answer": "",
        "chart": dict(_EMPTY_CHART),  # type: ignore[typeddict-item]
        "trace": [],
        "self_correct": self_correct,
        "schema_mode": schema_mode,
        "use_few_shot": use_few_shot,
        "enable_layer1": enable_layer1,
        "enable_readonly_authorizer": enable_readonly_authorizer,
        "db_path": str(db_path),
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }

    accumulated: dict[str, Any] = dict(initial_state)
    accumulated["trace"] = []

    async for update in agent_graph.astream(initial_state, stream_mode="updates"):
        if not isinstance(update, dict):
            continue
        for node_name, node_payload in update.items():
            if not isinstance(node_payload, dict):
                continue

            # 累加普通字段与数值统计字段
            for key, value in node_payload.items():
                if key == "trace":
                    if isinstance(value, list):
                        accumulated["trace"].extend(value)
                elif key in {"prompt_tokens", "completion_tokens", "total_tokens"}:
                    accumulated[key] = int(accumulated.get(key, 0)) + int(value or 0)
                else:
                    accumulated[key] = value

            node_traces = node_payload.get("trace") or []
            if node_traces:
                latest_trace = node_traces[-1]
                event: dict[str, Any] = {
                    "type": "node",
                    "node": latest_trace.get("node", node_name),
                    "status": latest_trace.get("status", "ok"),
                    "detail": latest_trace.get("detail", ""),
                    "ms": latest_trace.get("ms", 0.0),
                }
                if node_payload.get("sql"):
                    event["sql"] = node_payload["sql"]
                yield event

    latency_ms = round((time.perf_counter() - t_start) * 1000, 1)
    blocked = bool(accumulated.get("blocked", False))
    error = str(accumulated.get("error") or "")
    ok = (not blocked) and (not error)

    if write_log:
        _log_query(
            db_path=db_path,
            question=question,
            sql=str(accumulated.get("sql") or ""),
            ok=ok,
            retry_count=int(accumulated.get("retry_count") or 0),
            blocked=blocked,
            latency_ms=latency_ms,
        )

    yield {
        "type": "final",
        "question": question,
        "intent": accumulated.get("intent", ""),
        "sql": accumulated.get("sql", ""),
        "columns": accumulated.get("columns", []),
        "rows": accumulated.get("rows", []),
        "row_count": int(accumulated.get("row_count") or 0),
        "answer": accumulated.get("answer", ""),
        "chart": accumulated.get("chart", dict(_EMPTY_CHART)),
        "blocked": blocked,
        "block_reason": accumulated.get("block_reason", ""),
        "error": error,
        "retry_count": int(accumulated.get("retry_count") or 0),
        "trace": accumulated.get("trace", []),
        "latency_ms": latency_ms,
        "tokens": {
            "prompt_tokens": int(accumulated.get("prompt_tokens") or 0),
            "completion_tokens": int(accumulated.get("completion_tokens") or 0),
            "total_tokens": int(accumulated.get("total_tokens") or 0),
        },
    }


async def _arun_agent(**kwargs: Any) -> dict[str, Any]:
    final_payload: dict[str, Any] = {}
    async for event in astream_agent(**kwargs):
        if event.get("type") == "final":
            final_payload = {k: v for k, v in event.items() if k != "type"}
    return final_payload


def run_agent(
    question: str,
    history: list[dict[str, str]] | None = None,
    *,
    self_correct: bool = True,
    max_retries: int = 3,
    schema_mode: str = "full",
    use_few_shot: bool = True,
    enable_layer1: bool = True,
    enable_readonly_authorizer: bool = True,
    db_path: Path = DB_PATH,
    write_log: bool = True,
) -> dict[str, Any]:
    """同步执行入口（供评测脚本与同步 API 复用）。"""
    kwargs = {
        "question": question,
        "history": history,
        "self_correct": self_correct,
        "max_retries": max_retries,
        "schema_mode": schema_mode,
        "use_few_shot": use_few_shot,
        "enable_layer1": enable_layer1,
        "enable_readonly_authorizer": enable_readonly_authorizer,
        "db_path": db_path,
        "write_log": write_log,
    }

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_arun_agent(**kwargs))

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _arun_agent(**kwargs)).result()
