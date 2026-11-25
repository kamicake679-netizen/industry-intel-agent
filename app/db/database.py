"""SQLite 数据库建表、去重入库与动态 Schema 描述模块。"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.config import DB_PATH
from app.db.skill_normalizer import normalize_skills

EXPERIENCE_ENUM: tuple[str, ...] = (
    "经验不限",
    "应届生",
    "1年以内",
    "1-3年",
    "3-5年",
    "5年以上",
)

EDUCATION_ENUM: tuple[str, ...] = (
    "大专",
    "本科",
    "硕士",
    "博士",
    "不限",
)

SOURCE_ENUM: tuple[str, ...] = (
    "real_import",
    "demo_seed",
    "user_submit",
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS job_intel (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_hash TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    district TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    salary_min REAL,
    salary_max REAL,
    salary_avg REAL,
    experience TEXT NOT NULL DEFAULT '经验不限',
    education TEXT NOT NULL DEFAULT '不限',
    company_size TEXT NOT NULL DEFAULT '',
    industry TEXT NOT NULL DEFAULT '',
    fresh_grad_friendly INTEGER NOT NULL DEFAULT 0,
    raw_text TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'demo_seed',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_job_intel_city ON job_intel(city);
CREATE INDEX IF NOT EXISTS idx_job_intel_category ON job_intel(category);
CREATE INDEX IF NOT EXISTS idx_job_intel_experience ON job_intel(experience);
CREATE INDEX IF NOT EXISTS idx_job_intel_created_at ON job_intel(created_at);

CREATE TABLE IF NOT EXISTS job_skills (
    job_id INTEGER NOT NULL,
    skill TEXT NOT NULL,
    PRIMARY KEY (job_id, skill),
    FOREIGN KEY (job_id) REFERENCES job_intel(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_job_skills_skill ON job_skills(skill);

CREATE TABLE IF NOT EXISTS query_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
    question TEXT NOT NULL,
    sql TEXT NOT NULL DEFAULT '',
    ok INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    blocked INTEGER NOT NULL DEFAULT 0,
    latency_ms REAL NOT NULL DEFAULT 0
);
"""


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """获取可读写数据库连接，启用 Row 工厂与外键约束。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def db_session(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    """数据库连接上下文管理器，退出时自动关闭连接。"""
    conn = get_connection(db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH) -> None:
    """初始化数据库表结构与索引（幂等）。"""
    with db_session(db_path) as conn:
        conn.executescript(_SCHEMA_SQL)
        conn.commit()


def compute_job_hash(
    title: str,
    company: str = "",
    city: str = "",
    district: str = "",
    salary_min: float | None = None,
    salary_max: float | None = None,
) -> str:
    """根据岗位核心属性生成确定性去重哈希（32 位十六进制）。"""
    parts = [
        (title or "").strip().lower(),
        (company or "").strip().lower(),
        (city or "").strip().lower(),
        (district or "").strip().lower(),
        "" if salary_min is None else f"{float(salary_min):.1f}",
        "" if salary_max is None else f"{float(salary_max):.1f}",
    ]
    payload = "|".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _resolve_canonical_skill(conn: sqlite3.Connection, skill: str) -> str:
    """若库内已存在仅大小写不同的技能名，优先复用已有规范写法。"""
    row = conn.execute(
        "SELECT skill FROM job_skills WHERE LOWER(skill) = LOWER(?) LIMIT 1",
        (skill,),
    ).fetchone()
    return str(row["skill"]) if row else skill


def upsert_job(
    conn: sqlite3.Connection,
    job_data: dict[str, Any],
    skills: list[str] | None = None,
) -> tuple[int, bool]:
    """按 job_hash 幂等写入岗位与规范化技能表。

    返回 (job_id, is_new)。
    """
    title = str(job_data.get("title") or "").strip()
    if not title:
        raise ValueError("岗位名称 title 不能为空")

    company = str(job_data.get("company") or "").strip()
    city = str(job_data.get("city") or "").strip()
    district = str(job_data.get("district") or "").strip()
    salary_min = job_data.get("salary_min")
    salary_max = job_data.get("salary_max")
    salary_avg = job_data.get("salary_avg")

    if salary_avg is None and salary_min is not None and salary_max is not None:
        salary_avg = round((float(salary_min) + float(salary_max)) / 2.0, 1)

    job_hash = str(job_data.get("job_hash") or "").strip()
    if not job_hash:
        job_hash = compute_job_hash(
            title=title,
            company=company,
            city=city,
            district=district,
            salary_min=salary_min,
            salary_max=salary_max,
        )

    existing = conn.execute(
        "SELECT id FROM job_intel WHERE job_hash = ?",
        (job_hash,),
    ).fetchone()
    if existing is not None:
        return int(existing["id"]), False

    experience = str(job_data.get("experience") or "经验不限").strip()
    if experience not in EXPERIENCE_ENUM:
        experience = "经验不限"

    education = str(job_data.get("education") or "不限").strip()
    if education not in EDUCATION_ENUM:
        education = "不限"

    source = str(job_data.get("source") or "demo_seed").strip()
    if source not in SOURCE_ENUM:
        source = "demo_seed"

    created_at = str(job_data.get("created_at") or "").strip()
    columns = [
        "job_hash",
        "title",
        "company",
        "city",
        "district",
        "category",
        "salary_min",
        "salary_max",
        "salary_avg",
        "experience",
        "education",
        "company_size",
        "industry",
        "fresh_grad_friendly",
        "raw_text",
        "source",
    ]
    values: list[Any] = [
        job_hash,
        title,
        company,
        city,
        district,
        str(job_data.get("category") or "").strip(),
        salary_min,
        salary_max,
        salary_avg,
        experience,
        education,
        str(job_data.get("company_size") or "").strip(),
        str(job_data.get("industry") or "").strip(),
        1 if job_data.get("fresh_grad_friendly") else 0,
        str(job_data.get("raw_text") or "").strip(),
        source,
    ]
    if created_at:
        columns.append("created_at")
        values.append(created_at)

    placeholders = ", ".join(["?"] * len(columns))
    col_sql = ", ".join(columns)
    cur = conn.execute(
        f"INSERT INTO job_intel ({col_sql}) VALUES ({placeholders})",
        values,
    )
    job_id = int(cur.lastrowid)

    norm_skills = normalize_skills(skills or [])
    seen_canonical: set[str] = set()
    for skill in norm_skills:
        canonical = _resolve_canonical_skill(conn, skill)
        lower_c = canonical.lower()
        if lower_c in seen_canonical:
            continue
        seen_canonical.add(lower_c)
        conn.execute(
            "INSERT OR IGNORE INTO job_skills (job_id, skill) VALUES (?, ?)",
            (job_id, canonical),
        )

    return job_id, True


def get_job_by_id(job_id: int, db_path: Path = DB_PATH) -> dict[str, Any] | None:
    """按主键查询单条岗位详情及其关联技能列表。"""
    with db_session(db_path) as conn:
        row = conn.execute("SELECT * FROM job_intel WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        skill_rows = conn.execute(
            "SELECT skill FROM job_skills WHERE job_id = ? ORDER BY rowid ASC",
            (job_id,),
        ).fetchall()
        job = dict(row)
        job["skills"] = [str(r["skill"]) for r in skill_rows]
        return job


def get_schema_description(db_path: Path = DB_PATH, top_skills_limit: int = 40) -> str:
    """生成供 Text-to-SQL Prompt 注入的 Schema 描述、动态枚举值与高频技能清单。"""
    if not db_path.exists():
        return "数据库尚未初始化。"

    with db_session(db_path) as conn:
        total_jobs = int(conn.execute("SELECT COUNT(*) FROM job_intel").fetchone()[0])

        def _distinct_values(col: str) -> list[str]:
            rows = conn.execute(
                f"SELECT DISTINCT {col} FROM job_intel "
                f"WHERE {col} IS NOT NULL AND TRIM(CAST({col} AS TEXT)) != '' "
                f"ORDER BY {col} ASC"
            ).fetchall()
            return [str(r[0]) for r in rows]

        cities = _distinct_values("city")
        districts = _distinct_values("district")
        categories = _distinct_values("category")
        experiences = _distinct_values("experience")
        educations = _distinct_values("education")
        company_sizes = _distinct_values("company_size")
        industries = _distinct_values("industry")
        sources = _distinct_values("source")

        top_skills_rows = conn.execute(
            """
            SELECT skill, COUNT(*) AS cnt
            FROM job_skills
            GROUP BY skill
            ORDER BY cnt DESC, skill ASC
            LIMIT ?
            """,
            (top_skills_limit,),
        ).fetchall()
        top_skills_str = ", ".join(f"{r['skill']}({r['cnt']})" for r in top_skills_rows)

    lines = [
        f"当前 SQLite 数据库共有 {total_jobs} 条岗位记录，可供查询的业务表如下：",
        "",
        "1. 表 job_intel（岗位主表，每行代表 1 个招聘岗位）：",
        "   - id (INTEGER, PK): 岗位唯一自增主键",
        "   - job_hash (TEXT, UNIQUE): 去重指纹",
        "   - title (TEXT): 岗位名称，如 '大模型应用开发工程师'",
        "   - company (TEXT): 招聘企业名称（脱敏或真实企业名）",
        f"   - city (TEXT): 城市，实际取值：{cities}",
        f"   - district (TEXT): 行政区，实际取值：{districts}",
        f"   - category (TEXT): 岗位大类，实际取值：{categories}",
        "   - salary_min (REAL): 最低月薪（单位：K，即千元/月）",
        "   - salary_max (REAL): 最高月薪（单位：K，即千元/月）",
        "   - salary_avg (REAL): 平均月薪（单位：K，即千元/月；计算均薪优先用 AVG(salary_avg)）",
        f"   - experience (TEXT): 经验要求，实际取值：{experiences}",
        f"   - education (TEXT): 学历要求，实际取值：{educations}",
        f"   - company_size (TEXT): 企业规模，实际取值：{company_sizes}",
        f"   - industry (TEXT): 所属行业，实际取值：{industries}",
        "   - fresh_grad_friendly (INTEGER): 是否应届生/校招友好，0=否，1=是",
        "   - raw_text (TEXT): 原始 JD 文本",
        f"   - source (TEXT): 数据来源，实际取值：{sources}",
        "   - created_at (TEXT): 收录时间，格式 'YYYY-MM-DD HH:MM:SS'",
        "",
        "2. 表 job_skills（规范化技能关联表，第一范式）：",
        "   - job_id (INTEGER, FK -> job_intel.id): 关联岗位 ID",
        "   - skill (TEXT): 规范化后的标准技能名称（已统一大小写与同义词）",
        "   - 联合主键 (job_id, skill)。查询某技能岗位时请使用：JOIN job_skills js ON job_intel.id = js.job_id WHERE js.skill = '...'",
        f"   - 当前库内高频技能 TOP {top_skills_limit}：{top_skills_str or '暂无'}",
    ]
    return "\n".join(lines)
