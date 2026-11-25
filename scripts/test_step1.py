"""Step 1 自测脚本：验证入库条数、城市分布、技能归一、Schema 描述与 3 段 JD 抽取。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import DATA_DIR, DB_PATH
from app.db.database import db_session, get_schema_description
from app.db.seed_loader import init_and_seed_db, parse_salary_to_k
from app.db.skill_normalizer import normalize_skills
from app.pipeline.extractor import extract_and_save_job


def _test_unit_helpers() -> None:
    print("1. 测试技能同义词归一 & 薪资解析工具函数...")
    skills = normalize_skills(["大模型", "langgraph", "LangGraph", "function call", "rag", "RAG"])
    assert skills == ["LLM", "LangGraph", "Function Calling", "RAG"], f"技能归一异常: {skills}"

    assert parse_salary_to_k("15-25K·14薪") == (15.0, 25.0, 20.0)
    assert parse_salary_to_k("8000-12000元/月") == (8.0, 12.0, 10.0)
    assert parse_salary_to_k("24-36万/年") == (20.0, 30.0, 25.0)
    assert parse_salary_to_k("20-30万/年") == (16.7, 25.0, 20.9)
    print("   [PASS] 技能同义词归一与薪资换算全部准确。")


def _test_seed_and_schema() -> None:
    print("\n2. 测试 init_and_seed_db 幂等性与主库统计...")
    first = init_and_seed_db(DB_PATH)
    second = init_and_seed_db(DB_PATH)
    print(f"   首次导入: {first}")
    print(f"   二次导入: {second}")
    assert first["total_in_db"] > 0, "数据库为空"
    assert second["inserted"] == 0, f"二次导入未幂等: {second}"

    with db_session(DB_PATH) as conn:
        total = int(conn.execute("SELECT COUNT(*) FROM job_intel").fetchone()[0])
        cities = conn.execute(
            "SELECT city, COUNT(*) AS cnt, ROUND(AVG(salary_avg), 1) AS avg_k "
            "FROM job_intel GROUP BY city ORDER BY cnt DESC"
        ).fetchall()
        top_skills = conn.execute(
            "SELECT skill, COUNT(*) AS cnt FROM job_skills GROUP BY skill ORDER BY cnt DESC, skill ASC LIMIT 10"
        ).fetchall()
        dup_case = conn.execute(
            "SELECT LOWER(skill) AS s, COUNT(DISTINCT skill) AS variants "
            "FROM job_skills GROUP BY LOWER(skill) HAVING variants > 1"
        ).fetchall()

    print(f"   库内总条数: {total}")
    print("   城市分布:")
    for row in cities:
        print(f"     - {row['city']}: {row['cnt']} 条, 均薪 {row['avg_k']}K")
    print("   技能 TOP 10:")
    for row in top_skills:
        print(f"     - {row['skill']}: {row['cnt']}")

    assert len(dup_case) == 0, f"发现大小写重复技能: {[dict(r) for r in dup_case]}"
    print("   [PASS] job_skills 无任何大小写重复技能。")

    schema_desc = get_schema_description(DB_PATH)
    assert "job_intel" in schema_desc and "job_skills" in schema_desc
    assert "高频技能 TOP" in schema_desc
    print("\n3. get_schema_description() 预览（前 18 行）:")
    for line in schema_desc.splitlines()[:18]:
        print(f"   {line}")


def _test_llm_extraction() -> None:
    print("\n4. 测试 3 段不同风格 JD 的 LLM 结构化抽取（使用独立临时库，不污染主库）...")
    temp_db = DATA_DIR / "test_step1_extract.db"
    if temp_db.exists():
        temp_db.unlink()

    sample_jds = [
        {
            "name": "JD1（月薪K·14薪）",
            "text": (
                "【深圳南山区】星澜智能科技有限公司急招「大模型应用开发工程师」，"
                "薪资 15-25K·14薪，本科及以上，1-3年经验。"
                "要求熟练掌握 Python、LangGraph、RAG 检索增强生成及 Function Call，有 FastAPI 经验优先。"
            ),
            "expect_min": 15.0,
            "expect_max": 25.0,
            "expect_avg": 20.0,
        },
        {
            "name": "JD2（元/月 + 应届友好）",
            "text": (
                "【广州天河区】粤科数智招聘「AI 应用开发工程师（校招/应届生可投）」，"
                "月薪 8000-12000元/月，经验不限，欢迎 2026 届本科毕业生。"
                "技能要求：熟悉大模型 Prompt 调优、Dify、Python 和 SQL。"
            ),
            "expect_min": 8.0,
            "expect_max": 12.0,
            "expect_avg": 10.0,
            "expect_fresh": 1,
        },
        {
            "name": "JD3（万/年 年薪折算）",
            "text": (
                "【东莞松山湖】中科智能制造研究院招聘「Agent 系统架构师」，"
                "年薪 24-36万/年，要求 3-5年经验，硕士学历。"
                "负责多智能体编排、MCP 工具链与向量数据库 Milvus 架构设计。"
            ),
            "expect_min": 20.0,
            "expect_max": 30.0,
            "expect_avg": 25.0,
        },
    ]

    try:
        for idx, item in enumerate(sample_jds):
            res = extract_and_save_job(item["text"], source="user_submit", db_path=temp_db)
            job = res["job"]
            assert res["is_new"] is True
            print(
                f"   - {item['name']} -> title={job['title']}, city={job['city']}, "
                f"salary={job['salary_min']}-{job['salary_max']}K (avg={job['salary_avg']}K), "
                f"exp={job['experience']}, fresh={job['fresh_grad_friendly']}, skills={job['skills']}"
            )
            assert job["salary_min"] == item["expect_min"], f"{item['name']} 最低薪资错误: {job['salary_min']}"
            assert job["salary_max"] == item["expect_max"], f"{item['name']} 最高薪资错误: {job['salary_max']}"
            assert job["salary_avg"] == item["expect_avg"], f"{item['name']} 平均薪资错误: {job['salary_avg']}"
            if "expect_fresh" in item:
                assert job["fresh_grad_friendly"] == item["expect_fresh"]

            if idx == 0:
                dup_res = extract_and_save_job(item["text"], source="user_submit", db_path=temp_db)
                assert dup_res["is_new"] is False, "相同 JD 重复抽取应触发去重"

        with db_session(temp_db) as conn:
            dup_case = conn.execute(
                "SELECT LOWER(skill) AS s, COUNT(DISTINCT skill) AS variants "
                "FROM job_skills GROUP BY LOWER(skill) HAVING variants > 1"
            ).fetchall()
            assert len(dup_case) == 0, f"临时库发现大小写重复技能: {[dict(r) for r in dup_case]}"
        print("   [PASS] 3 段 JD 抽取、薪资换算与去重校验全部通过。")
    finally:
        if temp_db.exists():
            temp_db.unlink()


def main() -> int:
    print("=== Step 1 数据层与结构化抽取自测 ===")
    _test_unit_helpers()
    _test_seed_and_schema()
    _test_llm_extraction()
    print("\n=== Step 1 全部测试通过 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
