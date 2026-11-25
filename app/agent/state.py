"""LangGraph Text-to-SQL Agent 状态定义模块。"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class TraceEvent(TypedDict):
    """单个节点的执行轨迹条目。"""

    node: str
    status: str  # ok | retry | blocked | error
    detail: str
    ms: float


class ChartSpec(TypedDict):
    """前端迷你图表结构定义。"""

    type: str  # bar | line | none
    title: str
    labels: list[str]
    values: list[float | int]


class AgentState(TypedDict, total=False):
    """LangGraph 全局状态。"""

    # 输入与上下文
    question: str
    history: list[dict[str, str]]

    # 路由与 SQL 执行状态
    intent: str  # data_query | chitchat | unsafe
    sql: str
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    error: str
    retry_count: int
    max_retries: int
    blocked: bool
    block_reason: str

    # 输出结果
    answer: str
    chart: ChartSpec

    # 节点流式轨迹（使用 operator.add 累积，astream updates 仅推送当步新增项）
    trace: Annotated[list[TraceEvent], operator.add]

    # 运行控制与消融评测扩展字段
    self_correct: bool
    schema_mode: str  # full | minimal
    use_few_shot: bool
    enable_layer1: bool
    enable_readonly_authorizer: bool
    db_path: str

    # 统计指标
    prompt_tokens: Annotated[int, operator.add]
    completion_tokens: Annotated[int, operator.add]
    total_tokens: Annotated[int, operator.add]
