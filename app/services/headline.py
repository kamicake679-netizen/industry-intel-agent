"""头版头条生成与按日+城市缓存服务模块。"""

from __future__ import annotations

import json
import re
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openai import OpenAI

from app.config import DB_PATH, settings
from app.services.analytics import get_dashboard, normalize_city_filter
from app.services.ratelimit import rate_limiter

_CACHE_LOCK = threading.Lock()
_HEADLINE_CACHE: dict[tuple[str, str], dict[str, Any]] = {}

_HEADLINE_SYSTEM_PROMPT = """你是一家严肃财经/产业情报刊物《INDUSTRY INTEL》的主编。
请根据给定的招聘看板统计数字，撰写一条当日头版头条，严格输出 JSON 对象：
{
  "kicker": "栏目标签，例如 '头条 · 本周观察' 或 '头条 · 深圳观察'（不超过 14 个字）",
  "title": "主标题，一句有力的数据洞察结论（严格不超过 32 个汉字/字符）",
  "dek": "导语，两行以内的事实补充与对比分析（严格不超过 70 个汉字/字符）"
}

硬性要求：
1. 提到的所有数字（岗位数、均薪、占比、技能提及率等）必须严格来自输入 JSON，严禁编造或推测未提供的数字。
2. 所有薪资数值（avg_salary、median_salary、max_salary）单位均为【月薪 K（千元/月）】，例如 29.3 表示 29.3K。提及薪资时单位必须写作 K，严禁写成“万”“万元”或“元”。
3. 风格克制、客观、有新闻判断力，不使用感叹号、emoji 或夸张营销词。
4. 只输出合法 JSON，不要输出 Markdown 代码块。"""

_INVALID_WAN_RE = re.compile(r"\d+(?:\.\d+)?\s*万")


def _contains_invalid_wan_unit(text: str) -> bool:
    """检测文本中是否误将薪资 K 写成 '数字+万'。"""
    return bool(_INVALID_WAN_RE.search(text or ""))


def clear_headline_cache() -> None:
    """清空头条内存缓存（在新岗位入库或测试重置时调用）。"""
    with _CACHE_LOCK:
        _HEADLINE_CACHE.clear()


def _truncate_text(text: str, limit: int) -> str:
    cleaned = " ".join((text or "").split()).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(1, limit - 1)].rstrip("，。；：、 ") + "…"


def _build_fallback_headline(dashboard: dict[str, Any], city: str | None) -> dict[str, str]:
    """当未配置密钥、预算耗尽或 LLM 失败时，基于真实统计生成确定性模板头条。"""
    kpi = dashboard["kpi"]
    skills = dashboard["skills_top"]
    total = kpi["total_jobs"]
    avg_sal = kpi["avg_salary"]
    fresh_rate = kpi["fresh_grad_rate"]
    scope_label = city if city else "广深莞"

    top_skill = skills[0]["skill"] if skills else "LLM"
    top_rate = skills[0]["rate"] if skills else 0.0
    second_skill = skills[1]["skill"] if len(skills) > 1 else "RAG"

    kicker = f"头条 · {city}观察" if city else "头条 · 本周观察"
    if total == 0:
        return {
            "kicker": kicker,
            "title": f"{scope_label}暂无符合条件的在招岗位样本",
            "dek": "当前筛选条件下暂无数据，请切换城市或投稿新的岗位 JD 进行分析。",
        }

    avg_str = f"{avg_sal:.1f}K" if avg_sal is not None else "暂无"
    title = _truncate_text(
        f"{top_skill} 提及率达 {top_rate:.1f}%，{scope_label} AI 岗位均薪 {avg_str}",
        32,
    )
    dek = _truncate_text(
        f"基于 {total} 份在招岗位样本，应届友好岗占比 {fresh_rate:.1f}%，{top_skill} 与 {second_skill} 为核心技能需求。",
        70,
    )
    return {
        "kicker": kicker,
        "title": title,
        "dek": dek,
    }


def _strip_json_fences(content: str) -> str:
    text = (content or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def get_headline(
    city: str | None = None,
    db_path: Path = DB_PATH,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """获取指定城市的当日头版头条（按 日期+城市 缓存到内存）。"""
    norm_city = normalize_city_filter(city)
    today_str = date.today().isoformat()
    cache_key = (today_str, norm_city or "ALL")

    if not force_refresh:
        with _CACHE_LOCK:
            cached_item = _HEADLINE_CACHE.get(cache_key)
            if cached_item is not None:
                result = dict(cached_item)
                result["cached"] = True
                return result

    dashboard = get_dashboard(city=norm_city, db_path=db_path)
    summary_payload = {
        "scope": norm_city or "全部（深圳/广州/东莞）",
        "salary_unit": "K（千元/月，例如 29.3 表示 29.3K，绝不是 29.3 万）",
        "kpi": dashboard["kpi"],
        "skills_top5": dashboard["skills_top"][:5],
        "city_compare": dashboard["city_compare"],
    }

    fallback_used = False
    headline_fields: dict[str, str] | None = None

    api_key = settings.siliconflow_api_key.strip()
    can_call_llm = bool(api_key and api_key != "your_api_key_here" and dashboard["kpi"]["total_jobs"] > 0)

    if can_call_llm and rate_limiter.try_consume_budget(cost=1):
        try:
            client = OpenAI(api_key=api_key, base_url=settings.siliconflow_base_url)
            resp = client.chat.completions.create(
                model=settings.llm_model,
                messages=[
                    {"role": "system", "content": _HEADLINE_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"请根据以下真实统计数据生成头条：\n{json.dumps(summary_payload, ensure_ascii=False)}",
                    },
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
                max_tokens=300,
            )
            raw_content = resp.choices[0].message.content or ""
            parsed = json.loads(_strip_json_fences(raw_content))
            raw_title = str(parsed.get("title") or "").strip()
            raw_dek = str(parsed.get("dek") or "").strip()
            raw_kicker = str(parsed.get("kicker") or "").strip()
            if not raw_title or not raw_dek:
                raise ValueError("头条字段缺失")
            if _contains_invalid_wan_unit(raw_title) or _contains_invalid_wan_unit(raw_dek):
                raise ValueError("头条薪资单位误写为万")
            headline_fields = {
                "kicker": _truncate_text(raw_kicker or (f"头条 · {norm_city}观察" if norm_city else "头条 · 本周观察"), 16),
                "title": _truncate_text(raw_title, 32),
                "dek": _truncate_text(raw_dek, 70),
            }
        except Exception:
            headline_fields = None

    if headline_fields is None:
        fallback_used = True
        headline_fields = _build_fallback_headline(dashboard, norm_city)

    record = {
        "kicker": headline_fields["kicker"],
        "title": headline_fields["title"],
        "dek": headline_fields["dek"],
        "city": norm_city,
        "date": today_str,
        "cached": False,
        "fallback": fallback_used,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    with _CACHE_LOCK:
        _HEADLINE_CACHE[cache_key] = dict(record)

    return record
