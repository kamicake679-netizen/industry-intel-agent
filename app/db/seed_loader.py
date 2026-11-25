"""真实数据优先加载、薪资/枚举清洗与演示数据回退生成模块。"""

from __future__ import annotations

import csv
import json
import random
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.config import DATA_DIR, DB_PATH
from app.db.database import (
    EDUCATION_ENUM,
    EXPERIENCE_ENUM,
    compute_job_hash,
    db_session,
    init_db,
    upsert_job,
)
from app.db.skill_normalizer import normalize_skills

RAW_DIR = DATA_DIR / "raw"
SEED_JSON_PATH = DATA_DIR / "seed_jobs.json"

# 常见真实数据列名映射（小写归一化匹配 -> 标准字段）
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "title": ("title", "job_title", "position", "job_name", "岗位", "岗位名称", "职位", "职位名称"),
    "company": ("company", "company_name", "corp", "企业", "公司", "公司名称"),
    "city": ("city", "location", "work_city", "城市", "工作城市"),
    "district": ("district", "area", "zone", "区域", "行政区", "商圈"),
    "category": ("category", "job_category", "domain", "方向", "岗位大类", "类别", "职能"),
    "salary_min": ("salary_min", "min_salary", "最低薪资", "薪资下限"),
    "salary_max": ("salary_max", "max_salary", "最高薪资", "薪资上限"),
    "salary_avg": ("salary_avg", "avg_salary", "平均薪资"),
    "salary_raw": ("salary", "salary_raw", "salary_range", "薪资", "薪资范围", "月薪"),
    "experience": ("experience", "exp", "work_year", "经验", "工作经验", "经验要求"),
    "education": ("education", "edu", "degree", "学历", "学历要求"),
    "company_size": ("company_size", "size", "scale", "公司规模", "企业规模"),
    "industry": ("industry", "sector", "行业", "所属行业"),
    "fresh_grad_friendly": ("fresh_grad_friendly", "fresh_grad", "校招", "应届友好"),
    "skills": ("skills", "skill", "tags", "tech_stack", "技能", "技能要求", "技术栈"),
    "raw_text": ("raw_text", "jd", "description", "job_desc", "detail", "岗位描述", "职位描述"),
    "created_at": ("created_at", "publish_time", "pub_date", "发布时间", "采集时间"),
}


def parse_salary_to_k(salary_text: Any) -> tuple[float | None, float | None, float | None]:
    """将多种中文薪资写法统一换算为月薪 K（千元/月）。

    支持示例：
    - "15-25K·14薪" / "15k-25k*15薪" -> (15.0, 25.0, 20.0)
    - "8000-12000元/月" -> (8.0, 12.0, 10.0)
    - "1.5-2.5万/月" -> (15.0, 25.0, 20.0)
    - "20-30万/年" / "24-36万/年" -> 按 12 个月折算为 K
    - "300-400元/天" -> 按 21.75 个工作日折算为 K
    """
    if salary_text is None:
        return None, None, None
    text = str(salary_text).strip()
    if not text or any(k in text for k in ("面议", "保密", "若干")):
        return None, None, None

    # 先剥离 "·14薪" / "*15薪" / "14薪" 尾部，避免把月数误识别为上限数字
    cleaned = re.sub(r"[·*×xX]?\s*\d+\s*薪", "", text)
    cleaned = cleaned.replace("～", "-").replace("~", "-").replace("—", "-").replace("至", "-")

    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", cleaned)]
    if not nums:
        return None, None, None

    low = nums[0]
    high = nums[1] if len(nums) > 1 else nums[0]
    if low > high:
        low, high = high, low

    lower_text = cleaned.lower()
    is_year = any(k in lower_text for k in ("/年", "年薪", "每年", "一年"))
    is_day = any(k in lower_text for k in ("/天", "每天", "日薪", "/日"))
    is_wan = ("万" in lower_text) or ("w" in lower_text)
    is_k = "k" in lower_text or "千" in lower_text

    if is_year:
        if is_wan:
            # 万/年 -> 千/月：* 10 / 12
            low_k = low * 10.0 / 12.0
            high_k = high * 10.0 / 12.0
        elif low >= 1000:
            # 元/年 -> 千/月：/ 1000 / 12
            low_k = low / 12000.0
            high_k = high / 12000.0
        elif is_k:
            # K/年 -> 千/月：/ 12
            low_k = low / 12.0
            high_k = high / 12.0
        else:
            # 默认按万/年处理（如 "24-36/年"）
            low_k = low * 10.0 / 12.0
            high_k = high * 10.0 / 12.0
    elif is_day:
        if low >= 100:
            low_k = low * 21.75 / 1000.0
            high_k = high * 21.75 / 1000.0
        else:
            low_k = low
            high_k = high
    elif is_wan:
        low_k = low * 10.0
        high_k = high * 10.0
    elif is_k:
        low_k = low
        high_k = high
    elif low >= 1000 or high >= 1000:
        low_k = low / 1000.0
        high_k = high / 1000.0
    else:
        low_k = low
        high_k = high

    low_k = round(low_k, 1)
    high_k = round(high_k, 1)
    avg_k = round((low_k + high_k) / 2.0, 1)
    return low_k, high_k, avg_k


def normalize_experience(raw: Any) -> str:
    """将各类经验描述映射到标准经验枚举。"""
    text = str(raw or "").strip()
    if not text:
        return "经验不限"
    if text in EXPERIENCE_ENUM:
        return text
    lower = text.lower()
    if any(k in lower for k in ("应届", "校招", "在校", "实习", "毕业生")):
        return "应届生"
    if any(k in lower for k in ("不限", "无经验", "均可")):
        return "经验不限"
    if any(k in lower for k in ("1年以内", "1年以下", "一年以内", "一年以下", "0-1")):
        return "1年以内"
    if any(k in lower for k in ("1-3", "1~3", "2年", "3年以内", "1年以上", "2年以上")):
        return "1-3年"
    if any(k in lower for k in ("3-5", "3~5", "4年", "3年以上")):
        return "3-5年"
    if any(k in lower for k in ("5年以上", "5-10", "5~10", "6年", "8年", "10年", "5年及以上")):
        return "5年以上"
    return "经验不限"


def normalize_education(raw: Any) -> str:
    """将各类学历描述映射到标准学历枚举。"""
    text = str(raw or "").strip()
    if not text:
        return "不限"
    if text in EDUCATION_ENUM:
        return text
    if "博士" in text:
        return "博士"
    if "硕士" in text or "研究生" in text:
        return "硕士"
    if "本科" in text or "学士" in text or "统招" in text:
        return "本科"
    if "大专" in text or "专科" in text:
        return "大专"
    return "不限"


def detect_column_mapping(columns: list[str]) -> dict[str, str]:
    """根据列名自动识别真实数据字段映射（目标标准字段 -> 原始列名）。"""
    mapping: dict[str, str] = {}
    lower_to_orig = {col.strip().lower(): col for col in columns if col and col.strip()}

    for target, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias.lower() in lower_to_orig:
                mapping[target] = lower_to_orig[alias.lower()]
                break
    return mapping


def _convert_raw_record(row: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any] | None:
    """将真实数据中的单行记录映射为标准入库字典。"""

    def _get(field: str) -> Any:
        col = mapping.get(field)
        if col is not None and col in row:
            return row.get(col)
        return row.get(field)

    title = str(_get("title") or "").strip()
    if not title:
        return None

    s_min_val = _get("salary_min")
    s_max_val = _get("salary_max")
    s_avg_val = _get("salary_avg")
    s_raw_val = _get("salary_raw")

    salary_min: float | None = None
    salary_max: float | None = None
    salary_avg: float | None = None

    try:
        if s_min_val not in (None, ""):
            salary_min = float(s_min_val)
            if salary_min >= 1000:
                salary_min = round(salary_min / 1000.0, 1)
        if s_max_val not in (None, ""):
            salary_max = float(s_max_val)
            if salary_max >= 1000:
                salary_max = round(salary_max / 1000.0, 1)
        if s_avg_val not in (None, ""):
            salary_avg = float(s_avg_val)
            if salary_avg >= 1000:
                salary_avg = round(salary_avg / 1000.0, 1)
    except (TypeError, ValueError):
        salary_min = salary_max = salary_avg = None

    if salary_min is None and salary_max is None and s_raw_val:
        salary_min, salary_max, salary_avg = parse_salary_to_k(s_raw_val)
    elif salary_min is not None and salary_max is not None and salary_avg is None:
        salary_avg = round((salary_min + salary_max) / 2.0, 1)

    experience = normalize_experience(_get("experience"))
    education = normalize_education(_get("education"))
    raw_fresh = _get("fresh_grad_friendly")
    if raw_fresh in (1, "1", True, "true", "True", "是", "Y", "y"):
        fresh_grad = 1
    elif experience in ("应届生", "经验不限"):
        fresh_grad = 1 if experience == "应届生" else 0
    else:
        fresh_grad = 0

    skills = normalize_skills(_get("skills"))
    city = str(_get("city") or "").strip().rstrip("市")
    district = str(_get("district") or "").strip()
    company = str(_get("company") or "").strip()

    return {
        "job_hash": compute_job_hash(title, company, city, district, salary_min, salary_max),
        "title": title,
        "company": company,
        "city": city,
        "district": district,
        "category": str(_get("category") or "AI 应用开发").strip(),
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_avg": salary_avg,
        "experience": experience,
        "education": education,
        "company_size": str(_get("company_size") or "").strip(),
        "industry": str(_get("industry") or "").strip(),
        "fresh_grad_friendly": fresh_grad,
        "raw_text": str(_get("raw_text") or "").strip(),
        "skills": skills,
        "source": "real_import",
        "created_at": str(_get("created_at") or "").strip(),
    }


def _load_real_files(raw_dir: Path = RAW_DIR) -> tuple[list[dict[str, Any]], list[str]]:
    """扫描 data/raw/ 下的 CSV/JSON/JSONL 文件并解析，导入前打印字段映射。"""
    if not raw_dir.exists():
        return [], []

    files = sorted(
        p
        for p in raw_dir.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in {".csv", ".json", ".jsonl"}
    )
    if not files:
        return [], []

    records: list[dict[str, Any]] = []
    loaded_names: list[str] = []

    for path in files:
        suffix = path.suffix.lower()
        rows: list[dict[str, Any]] = []
        if suffix == ".csv":
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                rows = [dict(r) for r in reader]
        elif suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                rows = [r for r in data if isinstance(r, dict)]
            elif isinstance(data, dict) and isinstance(data.get("jobs"), list):
                rows = [r for r in data["jobs"] if isinstance(r, dict)]
        elif suffix == ".jsonl":
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            rows.append(obj)

        if not rows:
            continue

        columns = list(rows[0].keys())
        mapping = detect_column_mapping(columns)
        print(f"[seed_loader] 检测到真实数据文件 {path.name}（{len(rows)} 行），字段映射：{mapping}")
        loaded_names.append(path.name)
        for row in rows:
            converted = _convert_raw_record(row, mapping)
            if converted is not None:
                records.append(converted)

    return records, loaded_names


def _generate_demo_seed_jobs(seed_path: Path = SEED_JSON_PATH, count: int = 232) -> list[dict[str, Any]]:
    """当 data/raw/ 无真实数据时，生成确定性 232 条珠三角 AI/大模型行业演示数据。"""
    rng = random.Random(20260401)
    base_now = datetime(2026, 4, 15, 18, 0, 0)

    city_quotas = [("深圳", 105), ("广州", 85), ("东莞", 42)]
    districts_by_city = {
        "深圳": ["南山区", "福田区", "宝安区", "龙岗区", "龙华区"],
        "广州": ["天河区", "海珠区", "黄埔区", "越秀区", "番禺区"],
        "东莞": ["松山湖", "南城区", "东城区", "长安镇", "虎门镇"],
    }
    city_salary_factor = {"深圳": 1.12, "广州": 1.0, "东莞": 0.9}

    categories = [
        ("大模型应用开发", ["Python", "LLM", "RAG", "LangGraph", "FastAPI", "Function Calling", "Prompt Engineering", "VectorDB"], (18, 34)),
        ("Agent 架构师", ["LLM", "LangGraph", "Agent", "MCP", "Function Calling", "Python", "RAG", "Dify"], (24, 45)),
        ("算法工程师", ["Python", "PyTorch", "Transformers", "LoRA", "LLM", "CUDA", "RLHF", "NLP"], (22, 42)),
        ("AI 平台与推理工程", ["Python", "Docker", "Kubernetes", "vLLM", "CUDA", "FastAPI", "Linux", "Redis"], (20, 38)),
        ("数据与检索工程", ["Python", "SQL", "RAG", "Milvus", "Elasticsearch", "PostgreSQL", "Embedding", "Rerank"], (16, 30)),
        ("AI 产品经理", ["LLM", "Prompt Engineering", "RAG", "Agent", "Dify", "Coze", "LLM Eval"], (15, 28)),
        ("全栈 AI 开发", ["Python", "TypeScript", "React", "FastAPI", "LLM", "Docker", "SQL", "OpenAI API"], (16, 30)),
    ]

    title_templates = {
        "大模型应用开发": ["大模型应用开发工程师", "LLM 后端开发工程师", "RAG 知识库研发工程师", "AI 应用工程师"],
        "Agent 架构师": ["AI Agent 研发工程师", "多智能体系统工程师", "大模型架构师"],
        "算法工程师": ["大模型微调算法工程师", "NLP 算法工程师", "多模态算法工程师", "深度学习算法工程师"],
        "AI 平台与推理工程": ["大模型推理优化工程师", "MLOps 平台工程师", "AI 基础设施工程师"],
        "数据与检索工程": ["向量检索与 RAG 工程师", "AI 数据研发工程师", "Text-to-SQL 研发工程师"],
        "AI 产品经理": ["AI 应用产品经理", "大模型产品经理", "智能体产品经理"],
        "全栈 AI 开发": ["AI 全栈开发工程师", "AIGC 前端与服务端工程师", "智能工作流开发工程师"],
    }

    exp_weights = [
        ("经验不限", 0.14, 0.85),
        ("应届生", 0.16, 0.72),
        ("1年以内", 0.10, 0.82),
        ("1-3年", 0.28, 1.0),
        ("3-5年", 0.22, 1.25),
        ("5年以上", 0.10, 1.52),
    ]
    edu_choices = ["本科", "本科", "本科", "硕士", "硕士", "大专", "博士", "不限"]
    sizes = ["100-499人", "500-999人", "1000-9999人", "20-99人", "10000人以上"]
    industries = ["人工智能", "企业服务", "智能制造", "互联网", "跨境电商", "金融科技", "智慧医疗"]

    company_prefixes = [
        "星澜", "云岫", "深潜", "岭南", "智算", "明略", "数聚", "光年", "灵境", "锐思",
        "华南", "粤科", "微光", "矩量", "千帆", "飞鸟", "极客", "原力", "新质", "中科",
    ]
    company_suffixes = ["智能科技有限公司", "数智技术有限公司", "人工智能研究院", "信息科技有限公司", "软件有限公司"]

    jobs: list[dict[str, Any]] = []
    idx = 0
    for city, quota in city_quotas:
        for i in range(quota):
            idx += 1
            cat_name, cat_skills, (base_low, base_high) = categories[(idx + i) % len(categories)]
            title = rng.choice(title_templates[cat_name])
            company = f"{rng.choice(company_prefixes)}{rng.choice(company_suffixes)}"
            district = rng.choice(districts_by_city[city])

            # 确保广州有明确一批“经验不限 + 要求 RAG”样本
            if city == "广州" and i < 6:
                exp = "经验不限"
                exp_factor = 0.92
                cat_name = "大模型应用开发"
                cat_skills = categories[0][1]
            else:
                r = rng.random()
                cum = 0.0
                exp, exp_factor = "1-3年", 1.0
                for e_name, prob, factor in exp_weights:
                    cum += prob
                    if r <= cum:
                        exp, exp_factor = e_name, factor
                        break

            edu = rng.choice(edu_choices)
            if exp == "应届生" and edu == "大专":
                edu = "本科"

            fresh_grad = 1 if (exp in ("应届生", "经验不限") or rng.random() < 0.12) else 0
            c_factor = city_salary_factor[city]
            jitter = rng.uniform(0.9, 1.12)
            s_min = round(max(6.0, base_low * c_factor * exp_factor * jitter), 1)
            s_max = round(max(s_min + 3.0, base_high * c_factor * exp_factor * jitter), 1)
            s_avg = round((s_min + s_max) / 2.0, 1)

            chosen_skills = list(dict.fromkeys(rng.sample(cat_skills, k=min(len(cat_skills), rng.randint(4, 6)))))
            if city == "广州" and i < 6 and "RAG" not in chosen_skills:
                chosen_skills.insert(0, "RAG")
            chosen_skills = normalize_skills(chosen_skills)

            days_ago = rng.randint(0, 29)
            hours_ago = rng.randint(0, 23)
            created_dt = base_now - timedelta(days=days_ago, hours=hours_ago)
            created_at = created_dt.strftime("%Y-%m-%d %H:%M:%S")

            company_size = rng.choice(sizes)
            industry = rng.choice(industries)
            raw_text = (
                f"【{city}·{district}】{company} 招聘 {title}，"
                f"薪资 {s_min:.0f}-{s_max:.0f}K，要求{exp}，{edu}学历。"
                f"核心技术栈：{', '.join(chosen_skills)}。"
            )

            jobs.append(
                {
                    "job_hash": compute_job_hash(title, company, city, district, s_min, s_max),
                    "title": title,
                    "company": company,
                    "city": city,
                    "district": district,
                    "category": cat_name,
                    "salary_min": s_min,
                    "salary_max": s_max,
                    "salary_avg": s_avg,
                    "experience": exp,
                    "education": edu,
                    "company_size": company_size,
                    "industry": industry,
                    "fresh_grad_friendly": fresh_grad,
                    "skills": chosen_skills,
                    "raw_text": raw_text,
                    "source": "demo_seed",
                    "created_at": created_at,
                }
            )

    seed_path.parent.mkdir(parents=True, exist_ok=True)
    seed_path.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")
    return jobs


def _load_demo_seed(seed_path: Path = SEED_JSON_PATH) -> list[dict[str, Any]]:
    """读取或生成演示数据 seed_jobs.json。"""
    if seed_path.exists() and seed_path.stat().st_size > 2:
        try:
            data = json.loads(seed_path.read_text(encoding="utf-8"))
            if isinstance(data, list) and len(data) > 0:
                return data
        except json.JSONDecodeError:
            pass
    return _generate_demo_seed_jobs(seed_path=seed_path)


def init_and_seed_db(db_path: Path = DB_PATH) -> dict[str, Any]:
    """初始化数据库并按“真实数据优先，演示数据兜底”原则幂等入库。"""
    init_db(db_path)

    real_records, real_files = _load_real_files(RAW_DIR)
    if real_records:
        mode = "real_import"
        records = real_records
        files = real_files
    else:
        mode = "demo_seed"
        records = _load_demo_seed(SEED_JSON_PATH)
        files = [SEED_JSON_PATH.name]

    inserted = 0
    skipped = 0
    with db_session(db_path) as conn:
        for item in records:
            _, is_new = upsert_job(conn, item, item.get("skills", []))
            if is_new:
                inserted += 1
            else:
                skipped += 1
        conn.commit()
        total_in_db = int(conn.execute("SELECT COUNT(*) FROM job_intel").fetchone()[0])

    return {
        "mode": mode,
        "files": files,
        "total_input": len(records),
        "inserted": inserted,
        "skipped": skipped,
        "total_in_db": total_in_db,
    }
