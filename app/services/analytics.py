"""看板统计与数据聚合服务模块。"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.config import DB_PATH
from app.db.database import EXPERIENCE_ENUM, db_session


def normalize_city_filter(city: str | None) -> str | None:
    """规范化城市筛选参数；'全部'/'all'/空串统一返回 None。"""
    if city is None:
        return None
    text = str(city).strip()
    if not text or text.lower() == "all" or text == "全部":
        return None
    if len(text) >= 2 and text.endswith("市"):
        text = text[:-1]
    return text


def _round1(val: Any) -> float | None:
    if val is None:
        return None
    return round(float(val), 1)


def get_dashboard(city: str | None = None, db_path: Path = DB_PATH) -> dict[str, Any]:
    """聚合看板所需的 KPI、技能 TOP 10、三城对照、经验薪资、类别构成、最新入库与元信息。"""
    norm_city = normalize_city_filter(city)
    where_sql = "WHERE city = ?" if norm_city else ""
    params: tuple[Any, ...] = (norm_city,) if norm_city else ()

    with db_session(db_path) as conn:
        # 1. 全库城市列表与元信息
        city_rows = conn.execute(
            """
            SELECT city, COUNT(*) AS cnt
            FROM job_intel
            WHERE TRIM(city) != ''
            GROUP BY city
            ORDER BY cnt DESC, city ASC
            """
        ).fetchall()
        cities = [str(r["city"]) for r in city_rows]

        total_in_db = int(conn.execute("SELECT COUNT(*) FROM job_intel").fetchone()[0])
        source_rows = conn.execute(
            "SELECT source, COUNT(*) AS cnt FROM job_intel GROUP BY source"
        ).fetchall()
        source_counts = {str(r["source"]): int(r["cnt"]) for r in source_rows}
        max_created_row = conn.execute("SELECT MAX(created_at) AS max_ts FROM job_intel").fetchone()
        latest_ts = str(max_created_row["max_ts"] or "") if max_created_row else ""

        # 2. KPI 指标
        kpi_row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS total_jobs,
                AVG(salary_avg) AS avg_salary,
                SUM(CASE WHEN fresh_grad_friendly = 1 THEN 1 ELSE 0 END) AS fresh_grad_count,
                MAX(COALESCE(salary_max, salary_avg)) AS max_salary
            FROM job_intel
            {where_sql}
            """,
            params,
        ).fetchone()

        total_jobs = int(kpi_row["total_jobs"] or 0)
        avg_salary = _round1(kpi_row["avg_salary"])
        fresh_grad_count = int(kpi_row["fresh_grad_count"] or 0)
        fresh_grad_rate = round(fresh_grad_count * 100.0 / total_jobs, 1) if total_jobs > 0 else 0.0
        max_salary = _round1(kpi_row["max_salary"])

        # 中位薪资（Python 侧精确计算）
        salary_rows = conn.execute(
            f"""
            SELECT salary_avg
            FROM job_intel
            {where_sql + (" AND " if where_sql else "WHERE ")}salary_avg IS NOT NULL
            ORDER BY salary_avg ASC
            """,
            params,
        ).fetchall()
        salary_values = [float(r["salary_avg"]) for r in salary_rows]
        median_salary = round(float(statistics.median(salary_values)), 1) if salary_values else None

        # 最高薪岗位详情
        max_job_row = conn.execute(
            f"""
            SELECT id, title, company, city, category, salary_min, salary_max, salary_avg
            FROM job_intel
            {where_sql}
            ORDER BY COALESCE(salary_max, salary_avg, 0) DESC, COALESCE(salary_avg, 0) DESC, id DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        max_salary_job = dict(max_job_row) if max_job_row else None

        # 近 7 天新增（以库内最新入库时间为基准往前推 7 天，兼容演示数据日期与实时新投稿）
        weekly_new = 0
        if latest_ts and total_jobs > 0:
            try:
                anchor_dt = datetime.strptime(latest_ts[:19], "%Y-%m-%d %H:%M:%S")
                cutoff_str = (anchor_dt - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
                weekly_row = conn.execute(
                    f"""
                    SELECT COUNT(*) AS cnt
                    FROM job_intel
                    {where_sql + (" AND " if where_sql else "WHERE ")}created_at >= ?
                    """,
                    params + (cutoff_str,),
                ).fetchone()
                weekly_new = int(weekly_row["cnt"] or 0)
            except ValueError:
                weekly_new = 0

        # 3. 技能提及率 TOP 10
        if norm_city:
            skills_rows = conn.execute(
                """
                SELECT js.skill AS skill, COUNT(DISTINCT js.job_id) AS cnt
                FROM job_skills js
                JOIN job_intel ji ON ji.id = js.job_id
                WHERE ji.city = ?
                GROUP BY js.skill
                ORDER BY cnt DESC, js.skill ASC
                LIMIT 10
                """,
                (norm_city,),
            ).fetchall()
        else:
            skills_rows = conn.execute(
                """
                SELECT skill, COUNT(DISTINCT job_id) AS cnt
                FROM job_skills
                GROUP BY skill
                ORDER BY cnt DESC, skill ASC
                LIMIT 10
                """
            ).fetchall()

        skills_top = [
            {
                "skill": str(r["skill"]),
                "count": int(r["cnt"]),
                "rate": round(int(r["cnt"]) * 100.0 / total_jobs, 1) if total_jobs > 0 else 0.0,
            }
            for r in skills_rows
        ]

        # 4. 三城对照（始终返回全库各城市对照）
        compare_rows = conn.execute(
            """
            SELECT
                city,
                COUNT(*) AS job_count,
                AVG(salary_avg) AS avg_salary,
                SUM(CASE WHEN fresh_grad_friendly = 1 THEN 1 ELSE 0 END) AS fresh_grad_count
            FROM job_intel
            WHERE TRIM(city) != ''
            GROUP BY city
            ORDER BY job_count DESC, city ASC
            """
        ).fetchall()
        city_compare: list[dict[str, Any]] = []
        for r in compare_rows:
            c_name = str(r["city"])
            c_cnt = int(r["job_count"])
            c_fresh = int(r["fresh_grad_count"] or 0)
            c_salaries = [
                float(sr["salary_avg"])
                for sr in conn.execute(
                    "SELECT salary_avg FROM job_intel WHERE city = ? AND salary_avg IS NOT NULL",
                    (c_name,),
                ).fetchall()
            ]
            city_compare.append(
                {
                    "city": c_name,
                    "job_count": c_cnt,
                    "avg_salary": _round1(r["avg_salary"]),
                    "median_salary": round(float(statistics.median(c_salaries)), 1) if c_salaries else None,
                    "fresh_grad_count": c_fresh,
                    "fresh_grad_rate": round(c_fresh * 100.0 / c_cnt, 1) if c_cnt > 0 else 0.0,
                }
            )

        # 5. 经验—薪资阶梯
        exp_rows = conn.execute(
            f"""
            SELECT
                experience,
                COUNT(*) AS job_count,
                AVG(salary_avg) AS avg_salary
            FROM job_intel
            {where_sql}
            GROUP BY experience
            """,
            params,
        ).fetchall()
        exp_map = {str(r["experience"]): r for r in exp_rows}
        exp_salary: list[dict[str, Any]] = []
        for exp_name in EXPERIENCE_ENUM:
            row = exp_map.pop(exp_name, None)
            if row is not None:
                exp_salary.append(
                    {
                        "experience": exp_name,
                        "job_count": int(row["job_count"]),
                        "avg_salary": _round1(row["avg_salary"]),
                    }
                )
        for exp_name, row in exp_map.items():
            exp_salary.append(
                {
                    "experience": exp_name,
                    "job_count": int(row["job_count"]),
                    "avg_salary": _round1(row["avg_salary"]),
                }
            )

        # 6. 岗位大类构成（前 5 + 其他）
        cat_rows = conn.execute(
            f"""
            SELECT
                CASE WHEN TRIM(category) = '' THEN '未分类' ELSE category END AS cat_name,
                COUNT(*) AS cnt
            FROM job_intel
            {where_sql}
            GROUP BY cat_name
            ORDER BY cnt DESC, cat_name ASC
            """,
            params,
        ).fetchall()
        category_mix: list[dict[str, Any]] = []
        other_count = 0
        for idx, r in enumerate(cat_rows):
            cnt = int(r["cnt"])
            if idx < 5:
                category_mix.append(
                    {
                        "category": str(r["cat_name"]),
                        "count": cnt,
                        "rate": round(cnt * 100.0 / total_jobs, 1) if total_jobs > 0 else 0.0,
                    }
                )
            else:
                other_count += cnt
        if other_count > 0:
            category_mix.append(
                {
                    "category": "其他",
                    "count": other_count,
                    "rate": round(other_count * 100.0 / total_jobs, 1) if total_jobs > 0 else 0.0,
                }
            )

        # 7. 最新入库（最新 12 条）
        latest_rows = conn.execute(
            f"""
            SELECT
                id, title, company, city, district, category,
                salary_min, salary_max, salary_avg,
                experience, education, fresh_grad_friendly,
                source, created_at
            FROM job_intel
            {where_sql}
            ORDER BY created_at DESC, id DESC
            LIMIT 12
            """,
            params,
        ).fetchall()
        latest_ids = [int(r["id"]) for r in latest_rows]
        skills_by_job: dict[int, list[str]] = {jid: [] for jid in latest_ids}
        if latest_ids:
            placeholders = ", ".join(["?"] * len(latest_ids))
            s_rows = conn.execute(
                f"SELECT job_id, skill FROM job_skills WHERE job_id IN ({placeholders}) ORDER BY rowid ASC",
                latest_ids,
            ).fetchall()
            for sr in s_rows:
                skills_by_job[int(sr["job_id"])].append(str(sr["skill"]))

        latest = []
        for r in latest_rows:
            item = dict(r)
            item["skills"] = skills_by_job.get(int(r["id"]), [])
            latest.append(item)

        # 8. 元信息
        has_demo_data = source_counts.get("demo_seed", 0) > 0
        has_real_data = source_counts.get("real_import", 0) > 0
        if has_real_data and has_demo_data:
            source_note = "真实导入岗位 + 含演示数据（演示环境每日重置）"
        elif has_demo_data:
            source_note = "公开岗位建模样本（含演示数据，演示环境每日重置）"
        else:
            source_note = "真实采集岗位数据（演示环境新增投稿每日重置）"

    return {
        "city": norm_city,
        "cities": cities,
        "kpi": {
            "total_jobs": total_jobs,
            "avg_salary": avg_salary,
            "median_salary": median_salary,
            "fresh_grad_count": fresh_grad_count,
            "fresh_grad_rate": fresh_grad_rate,
            "max_salary": max_salary,
            "max_salary_job": max_salary_job,
            "weekly_new": weekly_new,
        },
        "skills_top": skills_top,
        "city_compare": city_compare,
        "exp_salary": exp_salary,
        "category_mix": category_mix,
        "latest": latest,
        "meta": {
            "sample_size": total_jobs,
            "total_in_db": total_in_db,
            "source_counts": source_counts,
            "has_demo_data": has_demo_data,
            "source_note": source_note,
            "updated_at": latest_ts or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    }
