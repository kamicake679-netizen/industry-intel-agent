"""三层 SQL 安全防线与只读执行引擎。

防线架构：
1. 第一层（预检与重写）：去注释、单语句校验、只读语句头校验、写/控制关键字拦截、敏感表拦截、非聚合查询自动补 LIMIT 50。
2. 第二层（数据库级只读隔离 + 授权器）：以 SQLite URI ``mode=ro`` 只读连接打开，并通过
   ``conn.set_authorizer()`` 仅放行 ``SQLITE_SELECT`` / ``SQLITE_READ`` / ``SQLITE_FUNCTION``，
   禁止读取 ``query_logs`` 及 ``sqlite_master`` 以外的系统对象。
3. 第三层（资源与超时护栏）：通过 ``conn.set_progress_handler()`` 限制单条查询最长 2 秒，
   并对结果行数设置硬上限，防止笛卡尔积或恶意大查询拖垮服务。
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from app.config import DB_PATH

DEFAULT_LIMIT = 50
MAX_ALLOWED_LIMIT = 200
DEFAULT_TIMEOUT_SECONDS = 2.0
_PROGRESS_HANDLER_STEPS = 1000

# 严禁出现的写操作、DDL、事务及控制关键字（在引号外按词边界匹配）
_FORBIDDEN_KEYWORDS = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "REPLACE",
    "TRUNCATE",
    "ATTACH",
    "DETACH",
    "PRAGMA",
    "VACUUM",
    "REINDEX",
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
    "SAVEPOINT",
    "RELEASE",
    "EXPLAIN",
    "ANALYZE",
    "LOAD_EXTENSION",
}

# 允许在只读连接中读取的白名单表（除此外的系统表与 query_logs 一律拒绝）
_ALLOWED_TABLES = {"job_intel", "job_skills", "sqlite_master"}

# 禁止在 SQL 中引用的敏感或内部表名
_FORBIDDEN_TABLE_PATTERN = re.compile(
    r"\b(?:query_logs|sqlite_schema|sqlite_temp_master|sqlite_temp_schema|sqlite_sequence|sqlite_stat\d*)\b",
    re.IGNORECASE,
)

# 禁止调用的危险函数
_FORBIDDEN_FUNCTIONS = {"load_extension"}

_AGG_FUNC_PATTERN = re.compile(
    r"\b(?:COUNT|AVG|SUM|MIN|MAX|TOTAL|GROUP_CONCAT)\s*\(",
    re.IGNORECASE,
)


def _strip_code_fences(sql: str) -> str:
    """移除模型可能输出的 Markdown 代码块包裹。"""
    text = (sql or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def strip_sql_comments(sql: str) -> str:
    """安全剥离 SQL 中的单行注释（--）与块注释（/* ... */），保留字符串字面量原貌。"""
    out: list[str] = []
    i = 0
    n = len(sql)
    in_single = False
    in_double = False

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_single:
            out.append(ch)
            if ch == "'":
                if nxt == "'":
                    out.append(nxt)
                    i += 2
                    continue
                in_single = False
            i += 1
            continue

        if in_double:
            out.append(ch)
            if ch == '"':
                if nxt == '"':
                    out.append(nxt)
                    i += 2
                    continue
                in_double = False
            i += 1
            continue

        if ch == "'":
            in_single = True
            out.append(ch)
            i += 1
            continue

        if ch == '"':
            in_double = True
            out.append(ch)
            i += 1
            continue

        # 单行注释 -- ...
        if ch == "-" and nxt == "-":
            i += 2
            while i < n and sql[i] not in ("\n", "\r"):
                i += 1
            out.append(" ")
            continue

        # 块注释 /* ... */
        if ch == "/" and nxt == "*":
            i += 2
            while i < n - 1 and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i = i + 2 if i < n - 1 else n
            out.append(" ")
            continue

        out.append(ch)
        i += 1

    return "".join(out).strip()


def _mask_string_literals(sql: str) -> str:
    """将单引号字符串字面量替换为等长占位符，便于准确做关键字与分号检测。"""
    out: list[str] = []
    i = 0
    n = len(sql)
    in_single = False

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if in_single:
            if ch == "'":
                if nxt == "'":
                    out.append("  ")
                    i += 2
                    continue
                in_single = False
                out.append("'")
            else:
                out.append(" ")
            i += 1
            continue

        if ch == "'":
            in_single = True
            out.append("'")
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def _has_top_level_clause(masked_sql: str, keyword_pattern: str) -> bool:
    """判断括号深度为 0 的顶层 SQL 中是否包含指定子句。"""
    depth = 0
    top_chars: list[str] = []
    for ch in masked_sql:
        if ch == "(":
            depth += 1
            top_chars.append(" ")
        elif ch == ")":
            depth = max(0, depth - 1)
            top_chars.append(" ")
        elif depth == 0:
            top_chars.append(ch)
        else:
            top_chars.append(" ")
    top_sql = "".join(top_chars)
    return re.search(keyword_pattern, top_sql, re.IGNORECASE) is not None


def _is_scalar_aggregate_query(masked_sql: str) -> bool:
    """判断是否为无 GROUP BY 的纯单行聚合查询（无需强行追加 LIMIT）。"""
    if _has_top_level_clause(masked_sql, r"\bGROUP\s+BY\b"):
        return False
    if _has_top_level_clause(masked_sql, r"\bUNION\b"):
        return False

    # 提取顶层最后一个主 SELECT 到 FROM 之间的投影段
    depth = 0
    top_chars: list[str] = []
    for ch in masked_sql:
        if ch == "(":
            depth += 1
            top_chars.append("(")
        elif ch == ")":
            depth = max(0, depth - 1)
            top_chars.append(")")
        elif depth == 0:
            top_chars.append(ch)
        else:
            top_chars.append(" ")
    top_sql = "".join(top_chars)

    select_matches = list(re.finditer(r"\bSELECT\b", top_sql, re.IGNORECASE))
    if not select_matches:
        return False
    last_select_start = select_matches[-1].end()
    from_match = re.search(r"\bFROM\b", top_sql[last_select_start:], re.IGNORECASE)
    select_segment = (
        masked_sql[last_select_start : last_select_start + from_match.start()]
        if from_match
        else masked_sql[last_select_start:]
    )
    return _AGG_FUNC_PATTERN.search(select_segment) is not None


def _enforce_limit(sql: str, masked_sql: str, default_limit: int = DEFAULT_LIMIT) -> str:
    """为非聚合查询自动补 LIMIT，并对过大的显式 LIMIT 进行上限钳制。"""
    has_limit = _has_top_level_clause(masked_sql, r"\bLIMIT\b")
    if has_limit:
        # 检查末尾或顶层 LIMIT 值是否超过硬上限
        limit_match = re.search(r"\bLIMIT\s+(\d+)(\s*(?:,|OFFSET)\s*\d+)?\s*$", sql, re.IGNORECASE)
        if limit_match:
            val = int(limit_match.group(1))
            if val > MAX_ALLOWED_LIMIT:
                suffix = limit_match.group(2) or ""
                return sql[: limit_match.start()] + f"LIMIT {default_limit}{suffix}"
        return sql

    if _is_scalar_aggregate_query(masked_sql):
        return sql

    return f"{sql.rstrip()} LIMIT {default_limit}"


def validate_and_rewrite_sql(
    sql: str,
    default_limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """第一层防线：SQL 预检与只读改写。

    返回字典包含：
    ``ok``, ``sql``, ``blocked``, ``block_reason``, ``error``。
    """
    cleaned = strip_sql_comments(_strip_code_fences(sql))
    if not cleaned:
        return {
            "ok": False,
            "sql": "",
            "blocked": True,
            "block_reason": "SQL 为空，无法执行。",
            "error": "SQL 为空",
        }

    # 去掉末尾允许出现的单个分号
    cleaned = re.sub(r";\s*$", "", cleaned).strip()
    if not cleaned:
        return {
            "ok": False,
            "sql": "",
            "blocked": True,
            "block_reason": "SQL 为空，无法执行。",
            "error": "SQL 为空",
        }

    masked = _mask_string_literals(cleaned)

    # 1. 拒绝多语句注入
    if ";" in masked:
        return {
            "ok": False,
            "sql": cleaned,
            "blocked": True,
            "block_reason": "检测到多语句 SQL（包含多余分号），已拦截。",
            "error": "多语句 SQL 被拦截",
        }

    # 2. 首词必须是 SELECT 或 WITH
    head_match = re.match(r"^\s*\(*\s*([A-Za-z_]+)", masked)
    first_word = head_match.group(1).upper() if head_match else ""
    if first_word not in {"SELECT", "WITH"}:
        return {
            "ok": False,
            "sql": cleaned,
            "blocked": True,
            "block_reason": f"仅允许只读 SELECT / WITH...SELECT 查询，检测到非法语句头：{first_word or '未知'}。",
            "error": "非只读 SQL 被拦截",
        }

    if first_word == "WITH" and not re.search(r"\bSELECT\b", masked, re.IGNORECASE):
        return {
            "ok": False,
            "sql": cleaned,
            "blocked": True,
            "block_reason": "WITH 语句必须最终执行只读 SELECT 查询。",
            "error": "非法 WITH 语句",
        }

    # 3. 拦截所有写操作/DDL/事务/PRAGMA 关键字
    tokens = set(re.findall(r"\b[A-Za-z_]+\b", masked.upper()))
    hit_forbidden = sorted(tokens & _FORBIDDEN_KEYWORDS)
    if hit_forbidden:
        return {
            "ok": False,
            "sql": cleaned,
            "blocked": True,
            "block_reason": f"检测到禁止的 SQL 关键字：{', '.join(hit_forbidden)}，本系统严格只读。",
            "error": f"包含禁止关键字: {', '.join(hit_forbidden)}",
        }

    # 4. 拦截敏感日志表与非法系统表
    table_hit = _FORBIDDEN_TABLE_PATTERN.search(masked)
    if table_hit:
        return {
            "ok": False,
            "sql": cleaned,
            "blocked": True,
            "block_reason": f"禁止读取内部日志表或受限系统对象：{table_hit.group(0)}。",
            "error": f"越权访问受限对象: {table_hit.group(0)}",
        }

    # 5. 自动补 LIMIT 50
    rewritten = _enforce_limit(cleaned, masked, default_limit=default_limit)
    return {
        "ok": True,
        "sql": rewritten,
        "blocked": False,
        "block_reason": "",
        "error": "",
    }


def _build_readonly_uri(db_path: Path) -> str:
    """构造跨平台（含 Windows 盘符与中文路径）SQLite 只读 URI。"""
    resolved = db_path.resolve()
    return f"{resolved.as_uri()}?mode=ro"


def execute_readonly_sql(
    sql: str,
    db_path: Path = DB_PATH,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_rows: int = MAX_ALLOWED_LIMIT,
) -> dict[str, Any]:
    """第二、三层防线：独立只读执行层（即使绕过第一层预检也能兜底）。

    - 使用 ``sqlite3.connect("file:...?mode=ro", uri=True)`` 只读打开；
    - 通过 ``conn.set_authorizer()`` 严格限制为 SELECT / READ / FUNCTION，并拦截 query_logs 及非法系统对象；
    - 通过 ``conn.set_progress_handler()`` 限制单条查询最长执行时间。
    """
    result: dict[str, Any] = {
        "ok": False,
        "sql": sql,
        "columns": [],
        "rows": [],
        "row_count": 0,
        "error": "",
        "blocked": False,
        "block_reason": "",
    }

    if not db_path.exists():
        result["error"] = f"数据库文件不存在：{db_path}"
        return result

    authorizer_denials: list[str] = []

    def _authorizer(
        action: int,
        arg1: str | None,
        arg2: str | None,
        dbname: str | None,
        source: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK

        if action == sqlite3.SQLITE_READ:
            table_name = (arg1 or "").strip().lower()
            if not table_name:
                return sqlite3.SQLITE_OK
            if table_name == "query_logs":
                authorizer_denials.append("授权器拒绝读取敏感日志表 query_logs")
                return sqlite3.SQLITE_DENY
            if table_name.startswith("sqlite_") and table_name != "sqlite_master":
                authorizer_denials.append(f"授权器拒绝读取受限系统对象 {table_name}")
                return sqlite3.SQLITE_DENY
            if table_name not in _ALLOWED_TABLES:
                authorizer_denials.append(f"授权器拒绝读取未授权表 {table_name}")
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        if action == sqlite3.SQLITE_FUNCTION:
            func_name = (arg2 or "").strip().lower()
            if func_name in _FORBIDDEN_FUNCTIONS:
                authorizer_denials.append(f"授权器拒绝调用受限函数 {func_name}")
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        authorizer_denials.append(f"授权器拒绝非只读操作（action={action}, target={arg1 or arg2 or ''}）")
        return sqlite3.SQLITE_DENY

    start_ts = time.monotonic()
    timed_out = False

    def _progress_handler() -> int:
        nonlocal timed_out
        if time.monotonic() - start_ts > timeout_seconds:
            timed_out = True
            return 1
        return 0

    conn: sqlite3.Connection | None = None
    try:
        uri = _build_readonly_uri(db_path)
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.set_authorizer(_authorizer)
        conn.set_progress_handler(_progress_handler, _PROGRESS_HANDLER_STEPS)

        cursor = conn.execute(sql)
        if cursor.description is None:
            result["ok"] = True
            return result

        columns = [str(col[0]) for col in cursor.description]
        fetched = cursor.fetchmany(max_rows)
        rows = [
            {col: row[col] for col in columns}
            for row in fetched
        ]

        result["ok"] = True
        result["columns"] = columns
        result["rows"] = rows
        result["row_count"] = len(rows)
        return result

    except (sqlite3.DatabaseError, sqlite3.Warning) as exc:
        err_msg = str(exc)
        if timed_out or "interrupted" in err_msg.lower():
            result["error"] = f"查询执行超时（超过 {timeout_seconds:.1f} 秒），已安全中止。"
            return result

        if authorizer_denials or "not authorized" in err_msg.lower() or "readonly database" in err_msg.lower():
            reason = authorizer_denials[0] if authorizer_denials else f"数据库只读隔离/授权器已拦截：{err_msg}"
            result["blocked"] = True
            result["block_reason"] = reason
            result["error"] = reason
            return result

        # execute() 遇到多语句时会直接抛出 sqlite3.Warning(3.10) / ProgrammingError(3.11+)
        if "one statement at a time" in err_msg.lower():
            result["blocked"] = True
            result["block_reason"] = "SQLite 执行层拒绝多语句执行，已拦截。"
            result["error"] = result["block_reason"]
            return result

        result["error"] = err_msg
        return result

    except Exception as exc:
        result["error"] = str(exc)
        return result

    finally:
        if conn is not None:
            try:
                conn.set_progress_handler(None, 0)
                conn.set_authorizer(None)
            except Exception:
                pass
            conn.close()


def execute_safe_sql(
    sql: str,
    db_path: Path = DB_PATH,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    default_limit: int = DEFAULT_LIMIT,
    enable_layer1: bool = True,
    enable_readonly_authorizer: bool = True,
) -> dict[str, Any]:
    """完整三层只读防线入口（支持通过开关适配 Step 7 消融对比评测）。"""
    final_sql = sql
    if enable_layer1:
        check = validate_and_rewrite_sql(sql, default_limit=default_limit)
        if not check["ok"]:
            return {
                "ok": False,
                "sql": check["sql"],
                "columns": [],
                "rows": [],
                "row_count": 0,
                "error": check["error"],
                "blocked": check["blocked"],
                "block_reason": check["block_reason"],
            }
        final_sql = check["sql"]

    if enable_readonly_authorizer:
        return execute_readonly_sql(
            final_sql,
            db_path=db_path,
            timeout_seconds=timeout_seconds,
        )

    # 仅在消融实验 A 组（关闭数据库授权器）时使用的回退只读路径
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(_build_readonly_uri(db_path), uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(final_sql)
        columns = [str(col[0]) for col in (cur.description or [])]
        fetched = cur.fetchmany(MAX_ALLOWED_LIMIT)
        rows = [{col: row[col] for col in columns} for row in fetched]
        return {
            "ok": True,
            "sql": final_sql,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "error": "",
            "blocked": False,
            "block_reason": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "sql": final_sql,
            "columns": [],
            "rows": [],
            "row_count": 0,
            "error": str(exc),
            "blocked": False,
            "block_reason": "",
        }
    finally:
        if conn is not None:
            conn.close()
