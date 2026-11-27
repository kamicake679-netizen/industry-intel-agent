"""情报简报 Markdown 生成与多平台 Webhook 推送服务模块。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import DB_PATH, settings
from app.services.analytics import get_dashboard, normalize_city_filter
from app.services.headline import get_headline


def generate_brief(city: str | None = None, db_path: Path = DB_PATH) -> str:
    """基于实时统计数据生成情报简报 Markdown 文本。"""
    norm_city = normalize_city_filter(city)
    dashboard = get_dashboard(city=norm_city, db_path=db_path)
    headline = get_headline(city=norm_city, db_path=db_path)

    kpi = dashboard["kpi"]
    meta = dashboard["meta"]
    scope = norm_city or "广深莞全库"
    now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")

    avg_str = f"{kpi['avg_salary']:.1f}K" if kpi["avg_salary"] is not None else "--"
    med_str = f"{kpi['median_salary']:.1f}K" if kpi["median_salary"] is not None else "--"
    max_str = f"{kpi['max_salary']:.1f}K" if kpi["max_salary"] is not None else "--"
    max_job = kpi.get("max_salary_job") or {}
    max_job_note = (
        f"（{max_job.get('city', '')} · {max_job.get('title', '')}）"
        if max_job
        else ""
    )

    lines = [
        f"# INDUSTRY INTEL 行业情报简报 · {scope}",
        f"> 生成时间：{now_str} ｜ 样本量：n={meta['sample_size']} ｜ {meta['source_note']}",
        "",
        f"## {headline['kicker']}：{headline['title']}",
        headline["dek"],
        "",
        "## 核心指标",
        f"- **收录岗位**：{kpi['total_jobs']} 份（近 7 天新增 {kpi['weekly_new']} 份）",
        f"- **平均月薪**：{avg_str}（中位数 {med_str}）",
        f"- **应届友好**：{kpi['fresh_grad_rate']:.1f}%（{kpi['fresh_grad_count']} / {kpi['total_jobs']} 份）",
        f"- **最高月薪**：{max_str}{max_job_note}",
        "",
        "## 热门技能 TOP 5",
    ]

    for idx, item in enumerate(dashboard["skills_top"][:5], start=1):
        lines.append(f"{idx}. **{item['skill']}**：提及率 {item['rate']:.1f}%（{item['count']} 份）")

    lines.extend(
        [
            "",
            "## 城市对比",
            "| 城市 | 岗位数 | 平均月薪 | 中位月薪 | 应届友好占比 |",
            "| :--- | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in dashboard["city_compare"]:
        c_avg = f"{row['avg_salary']:.1f}K" if row["avg_salary"] is not None else "--"
        c_med = f"{row['median_salary']:.1f}K" if row["median_salary"] is not None else "--"
        lines.append(
            f"| {row['city']} | {row['job_count']} | {c_avg} | {c_med} | {row['fresh_grad_rate']:.1f}% |"
        )

    lines.extend(["", "## 经验—薪资阶梯"])
    for row in dashboard["exp_salary"]:
        e_avg = f"{row['avg_salary']:.1f}K" if row["avg_salary"] is not None else "--"
        lines.append(f"- {row['experience']}：{e_avg}（{row['job_count']} 份）")

    lines.extend(
        [
            "",
            "---",
            f"*数据出处：{meta['source_note']} ｜ 数据更新时间：{meta['updated_at']}*",
        ]
    )
    return "\n".join(lines)


def detect_webhook_platform(url: str) -> str:
    """根据 Webhook URL 域名识别推送平台。"""
    host = (urlparse(url).hostname or "").lower()
    if "feishu.cn" in host or "larksuite.com" in host:
        return "feishu"
    if "dingtalk.com" in host:
        return "dingtalk"
    if "weixin.qq.com" in host:
        return "wecom"
    return "generic"


def _build_webhook_payload(platform: str, md: str, title: str) -> dict[str, Any]:
    if platform == "feishu":
        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": "red",
                },
                "elements": [{"tag": "markdown", "content": md}],
            },
        }
    if platform == "dingtalk":
        return {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": md},
        }
    if platform == "wecom":
        return {
            "msgtype": "markdown",
            "markdown": {"content": md},
        }
    return {"title": title, "markdown": md}


def push_webhook(
    md: str,
    url: str | None = None,
    title: str = "INDUSTRY INTEL 每日行业情报简报",
) -> dict[str, Any]:
    """推送 Markdown 简报到 Webhook；未配置地址时返回预览模式。"""
    target_url = (url or settings.webhook_url or "").strip()
    if not target_url:
        return {
            "mode": "preview",
            "sent": False,
            "platform": "none",
            "status_code": None,
            "markdown": md,
            "message": "未配置 Webhook 地址，已返回简报预览模式。",
        }

    parsed = urlparse(target_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Webhook URL 格式无效，必须以 http:// 或 https:// 开头。")

    platform = detect_webhook_platform(target_url)
    payload = _build_webhook_payload(platform, md, title)

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(target_url, json=payload)
        sent_ok = 200 <= resp.status_code < 300
        return {
            "mode": "pushed",
            "sent": sent_ok,
            "platform": platform,
            "status_code": resp.status_code,
            "markdown": md,
            "message": "简报推送成功。" if sent_ok else f"Webhook 返回状态码 {resp.status_code}：{resp.text[:120]}",
        }
    except Exception as exc:
        return {
            "mode": "pushed",
            "sent": False,
            "platform": platform,
            "status_code": None,
            "markdown": md,
            "message": f"Webhook 推送失败：{exc}",
        }
