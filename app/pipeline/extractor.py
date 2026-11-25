"""LLM 结构化 JD 抽取管道：Pydantic 校验、薪资折算、失败重试与入库。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openai import OpenAI
from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import DB_PATH, settings
from app.db.database import EDUCATION_ENUM, EXPERIENCE_ENUM, db_session, get_job_by_id, init_db, upsert_job
from app.db.seed_loader import normalize_education, normalize_experience, parse_salary_to_k
from app.db.skill_normalizer import normalize_skills

_EXTRACTION_SYSTEM_PROMPT = """你是一名专业的行业招聘情报结构化抽取引擎。
请从用户提供的非结构化岗位 JD 文本中提取核心字段，并严格返回合法的 JSON 对象（不要输出任何 Markdown 代码块或额外解释）。

输出 JSON 字段规范如下：
{
  "title": "岗位名称（必填，字符串）",
  "company": "公司名称（未提及则填空字符串）",
  "city": "城市名称，不带'市'字，如 '深圳'、'广州'、'东莞'（未提及则填空字符串）",
  "district": "行政区或商圈，如 '南山区'（未提及则填空字符串）",
  "category": "岗位大类，如 '大模型应用开发'、'Agent 架构师'、'算法工程师'、'AI 平台与推理工程'、'数据与检索工程'、'AI 产品经理'、'全栈 AI 开发'",
  "salary_raw": "原文中出现的完整薪资字符串，如 '15-25K·14薪' 或 '24-36万/年'（未提及填空字符串）",
  "salary_min": 最低月薪数值（浮点数，单位必须是 K，即千元/月，未提及填 null）,
  "salary_max": 最高月薪数值（浮点数，单位必须是 K，即千元/月，未提及填 null）,
  "salary_avg": 平均月薪数值（浮点数，单位必须是 K，即千元/月，未提及填 null）,
  "experience": "必须严格取自以下枚举之一：经验不限 / 应届生 / 1年以内 / 1-3年 / 3-5年 / 5年以上",
  "education": "必须严格取自以下枚举之一：大专 / 本科 / 硕士 / 博士 / 不限",
  "company_size": "企业规模，如 '100-499人'（未提及填空字符串）",
  "industry": "所属行业，如 '人工智能'（未提及填空字符串）",
  "fresh_grad_friendly": 是否欢迎应届生或校招友好（整数 1 或 0；若体验为应届生、可投递、校招、无经验要求则为 1）,
  "skills": ["简洁独立的技能关键词，如 Python, RAG, LangGraph, MCP, Milvus（不要带“熟悉/掌握/架构设计/工具链/调优”等修饰词）"]
}

薪资换算硬性规则（务必严格执行）：
1. 所有薪资数值统一换算为【月薪 K（千元/月）】。
2. 例如 "15-25K·14薪" -> 月薪为 15.0 ~ 25.0（14薪不折算入单月，保持月薪 K），salary_avg = 20.0。
3. 例如 "8000-12000元/月" -> 除以 1000，换算为 8.0 ~ 12.0，salary_avg = 10.0。
4. 例如 "20-30万/年" 或 "24-36万/年" -> 除以 12 个月后再乘以 10 换算为月薪 K（24-36万/年 = 20.0 ~ 30.0K，salary_avg = 25.0K）。
"""


class ExtractedJob(BaseModel):
    """结构化抽取后的岗位数据模型。"""

    title: str = Field(..., min_length=1, description="岗位名称")
    company: str = Field(default="")
    city: str = Field(default="")
    district: str = Field(default="")
    category: str = Field(default="大模型应用开发")
    salary_raw: str = Field(default="")
    salary_min: float | None = Field(default=None)
    salary_max: float | None = Field(default=None)
    salary_avg: float | None = Field(default=None)
    experience: str = Field(default="经验不限")
    education: str = Field(default="不限")
    company_size: str = Field(default="")
    industry: str = Field(default="")
    fresh_grad_friendly: int = Field(default=0)
    skills: list[str] = Field(default_factory=list)

    @field_validator("title", "company", "city", "district", "category", "salary_raw", "company_size", "industry", mode="before")
    @classmethod
    def _clean_str(cls, v: Any) -> str:
        return "" if v is None else str(v).strip()

    @field_validator("city", mode="after")
    @classmethod
    def _clean_city(cls, v: str) -> str:
        return v[:-1] if len(v) >= 2 and v.endswith("市") else v

    @field_validator("experience", mode="before")
    @classmethod
    def _norm_exp(cls, v: Any) -> str:
        val = normalize_experience(v)
        return val if val in EXPERIENCE_ENUM else "经验不限"

    @field_validator("education", mode="before")
    @classmethod
    def _norm_edu(cls, v: Any) -> str:
        val = normalize_education(v)
        return val if val in EDUCATION_ENUM else "不限"

    @field_validator("fresh_grad_friendly", mode="before")
    @classmethod
    def _norm_fresh(cls, v: Any) -> int:
        if isinstance(v, bool):
            return 1 if v else 0
        if isinstance(v, (int, float)):
            return 1 if int(v) != 0 else 0
        if isinstance(v, str) and v.strip().lower() in {"1", "true", "是", "yes"}:
            return 1
        return 0

    @field_validator("skills", mode="before")
    @classmethod
    def _norm_skills(cls, v: Any) -> list[str]:
        return normalize_skills(v)

    @model_validator(mode="after")
    def _calibrate_salary(self) -> "ExtractedJob":
        # 如果 salary_raw 明确包含年薪/元/月等信息，用程序规则交叉校准，避免 LLM 忘记除以 12 或 1000
        if self.salary_raw:
            parsed_min, parsed_max, parsed_avg = parse_salary_to_k(self.salary_raw)
            if parsed_min is not None and parsed_max is not None:
                if (
                    self.salary_min is None
                    or self.salary_max is None
                    or any(k in self.salary_raw for k in ("年", "元", "万", "w", "W"))
                ):
                    self.salary_min = parsed_min
                    self.salary_max = parsed_max
                    self.salary_avg = parsed_avg
                    return self

        if self.salary_min is not None and self.salary_min >= 1000:
            self.salary_min = round(self.salary_min / 1000.0, 1)
        if self.salary_max is not None and self.salary_max >= 1000:
            self.salary_max = round(self.salary_max / 1000.0, 1)

        if self.salary_min is not None and self.salary_max is not None:
            if self.salary_min > self.salary_max:
                self.salary_min, self.salary_max = self.salary_max, self.salary_min
            self.salary_avg = round((self.salary_min + self.salary_max) / 2.0, 1)
        elif self.salary_min is not None:
            self.salary_max = self.salary_min
            self.salary_avg = self.salary_min
        elif self.salary_max is not None:
            self.salary_min = self.salary_max
            self.salary_avg = self.salary_max

        return self


def _strip_json_fences(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def extract_job_from_text(raw_text: str) -> ExtractedJob:
    """调用 LLM JSON 模式将非结构化 JD 抽取为 ExtractedJob（失败自动重试 1 次）。"""
    text = raw_text.strip()
    if not text:
        raise ValueError("JD 文本不能为空")

    api_key = settings.siliconflow_api_key.strip()
    if not api_key or api_key == "your_api_key_here":
        raise RuntimeError("未配置有效的 SILICONFLOW_API_KEY，无法调用 LLM 进行抽取")

    client = OpenAI(api_key=api_key, base_url=settings.siliconflow_base_url)

    messages: list[dict[str, str]] = [
        {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
        {"role": "user", "content": f"请抽取以下 JD 文本：\n\n{text}"},
    ]

    last_error: Exception | None = None
    for attempt in range(2):
        response = client.chat.completions.create(
            model=settings.llm_model,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=800,
        )
        content = response.choices[0].message.content or ""
        cleaned = _strip_json_fences(content)
        try:
            payload = json.loads(cleaned)
            return ExtractedJob.model_validate(payload)
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": f"上一次输出的 JSON 解析或字段校验失败，错误信息：{exc}。请修正并仅返回合法 JSON 对象。",
                    }
                )

    raise ValueError(f"JD 结构化抽取失败（已重试 1 次）：{last_error}")


def extract_and_save_job(
    raw_text: str,
    source: str = "user_submit",
    db_path: Path = DB_PATH,
) -> dict[str, Any]:
    """抽取 JD 并幂等写入数据库，返回 {"job": dict, "is_new": bool}。"""
    extracted = extract_job_from_text(raw_text)
    init_db(db_path)

    job_payload = extracted.model_dump(exclude={"salary_raw"})
    job_payload["raw_text"] = raw_text.strip()
    job_payload["source"] = source

    with db_session(db_path) as conn:
        job_id, is_new = upsert_job(conn, job_payload, extracted.skills)
        conn.commit()

    saved_job = get_job_by_id(job_id, db_path=db_path)
    return {
        "job": saved_job,
        "is_new": is_new,
    }
