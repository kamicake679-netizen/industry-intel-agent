"""Step 3 FastAPI 后端全量接口自测脚本。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# 确保从任意工作目录执行都能导入 app
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import DB_PATH  # noqa: E402
from app.db.database import db_session  # noqa: E402
from app.main import app  # noqa: E402
from app.services.headline import clear_headline_cache  # noqa: E402
from app.services.ratelimit import rate_limiter  # noqa: E402


def _parse_sse_lines(lines: list[str]) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    cur_event = "message"
    data_buf: list[str] = []

    for raw in lines:
        line = raw.rstrip("\r\n")
        if line == "":
            if data_buf:
                payload_str = "\n".join(data_buf)
                events.append((cur_event, json.loads(payload_str)))
                cur_event = "message"
                data_buf = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            cur_event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_buf.append(line.split(":", 1)[1].strip())

    if data_buf:
        events.append((cur_event, json.loads("\n".join(data_buf))))
    return events


def main() -> None:
    rate_limiter.reset()
    clear_headline_cache()
    inserted_test_job_id: int | None = None

    try:
        with TestClient(app) as client:
            # 1. 基础页面与 OpenAPI 文档
            r_root = client.get("/")
            assert r_root.status_code == 200, f"GET / 失败: {r_root.status_code}"
            r_docs = client.get("/docs")
            assert r_docs.status_code == 200, f"GET /docs 失败: {r_docs.status_code}"
            r_openapi = client.get("/openapi.json")
            assert r_openapi.status_code == 200
            paths = set(r_openapi.json().get("paths", {}).keys())
            expected_paths = {
                "/api/health",
                "/api/dashboard",
                "/api/headline",
                "/api/chat",
                "/api/chat/stream",
                "/api/extract",
                "/api/brief",
            }
            missing = expected_paths - paths
            assert not missing, f"OpenAPI 缺少路由: {missing}"
            print(f"[1/9] 基础路由与 OpenAPI 校验通过（{len(expected_paths)} 个接口全部就绪）")

            # 2. /api/health
            r_health = client.get("/api/health")
            assert r_health.status_code == 200
            health = r_health.json()
            assert health["status"] == "ok"
            assert health["total_jobs"] >= 232
            assert "daily_remaining" in health["budget"]
            print(f"[2/9] /api/health 通过: total_jobs={health['total_jobs']}, remaining={health['budget']['daily_remaining']}")

            # 3. /api/dashboard（全量 + 深圳筛选）
            r_dash = client.get("/api/dashboard")
            assert r_dash.status_code == 200
            dash = r_dash.json()
            for key in ("kpi", "skills_top", "city_compare", "exp_salary", "category_mix", "latest", "meta"):
                assert key in dash, f"dashboard 缺少字段: {key}"
            assert dash["kpi"]["total_jobs"] >= 232
            assert len(dash["skills_top"]) == 10
            assert len(dash["latest"]) == 12

            r_sz = client.get("/api/dashboard", params={"city": "深圳"})
            assert r_sz.status_code == 200
            dash_sz = r_sz.json()
            assert dash_sz["city"] == "深圳"
            assert 0 < dash_sz["kpi"]["total_jobs"] < dash["kpi"]["total_jobs"]
            print(
                f"[3/9] /api/dashboard 通过: 全库={dash['kpi']['total_jobs']} 条, "
                f"深圳={dash_sz['kpi']['total_jobs']} 条, 均薪={dash_sz['kpi']['avg_salary']}K"
            )

            # 4. /api/headline（首次生成 + 二次命中缓存）
            r_hl1 = client.get("/api/headline")
            assert r_hl1.status_code == 200
            hl1 = r_hl1.json()
            assert 0 < len(hl1["title"]) <= 32, f"头条标题超长: {hl1['title']}"
            assert 0 < len(hl1["dek"]) <= 70, f"头条导语超长: {hl1['dek']}"
            assert hl1["cached"] is False

            r_hl2 = client.get("/api/headline")
            assert r_hl2.status_code == 200
            hl2 = r_hl2.json()
            assert hl2["cached"] is True
            print(f"[4/9] /api/headline 通过: 「{hl1['title']}」（fallback={hl1['fallback']}, cached2={hl2['cached']}）")

            # 5. /api/chat（同步问询）
            r_chat = client.post("/api/chat", json={"question": "三城均薪对比", "history": []})
            assert r_chat.status_code == 200, f"/api/chat 失败: {r_chat.text}"
            chat_data = r_chat.json()
            assert chat_data.get("answer")
            assert chat_data.get("sql")
            assert len(chat_data.get("trace", [])) >= 3
            print(f"[5/9] /api/chat 通过: row_count={chat_data['row_count']}, trace={len(chat_data['trace'])} 步")

            # 6. /api/chat/stream（SSE 流式问询）
            with client.stream(
                "POST",
                "/api/chat/stream",
                json={"question": "深圳和广州哪个均薪更高", "history": []},
            ) as resp:
                assert resp.status_code == 200
                raw_lines = list(resp.iter_lines())
            events = _parse_sse_lines(raw_lines)
            node_events = [e for e in events if e[0] == "node"]
            final_events = [e for e in events if e[0] == "final"]
            assert len(node_events) >= 1, f"未收到 node 事件: {events}"
            assert len(final_events) == 1, f"应收到恰好 1 个 final 事件: {events}"
            final_payload = final_events[0][1]
            assert final_payload.get("answer")
            assert len(final_payload.get("trace", [])) >= 3
            print(
                f"[6/9] /api/chat/stream 通过: node 事件 {len(node_events)} 个, "
                f"final 事件 1 个, 节点={[e[1]['node'] for e in node_events]}"
            )

            # 7. /api/extract（JD 结构化抽取投稿）
            sample_jd = (
                "【深圳南山区】深智未来科技有限公司招聘大模型应用开发工程师，"
                "年薪 24-36万/年，本科及以上学历，1-3年经验，欢迎优秀应届生投递。"
                "技术栈要求：Python、LangGraph、RAG、FastAPI、Milvus。"
            )
            r_ext = client.post("/api/extract", json={"raw_text": sample_jd})
            assert r_ext.status_code == 200, f"/api/extract 失败: {r_ext.text}"
            ext_data = r_ext.json()
            job = ext_data["job"]
            assert job["city"] == "深圳"
            assert job["salary_min"] == 20.0 and job["salary_max"] == 30.0, f"年薪折算异常: {job}"
            if ext_data["is_new"]:
                inserted_test_job_id = int(job["id"])
            print(
                f"[7/9] /api/extract 通过: title={job['title']}, "
                f"salary={job['salary_min']}-{job['salary_max']}K, is_new={ext_data['is_new']}"
            )

            # 8. /api/brief（预览模式）
            r_brief = client.post("/api/brief", json={"city": None, "push": False})
            assert r_brief.status_code == 200
            brief_data = r_brief.json()
            assert brief_data["mode"] == "preview"
            assert "INDUSTRY INTEL 行业情报简报" in brief_data["markdown"]
            print(f"[8/9] /api/brief 通过: mode={brief_data['mode']}, len={len(brief_data['markdown'])} 字符")

            # 9. 参数校验 422 与限流 429 测试
            r_422 = client.post("/api/chat", json={"question": ""})
            assert r_422.status_code == 422
            err_422 = r_422.json()
            assert err_422["error"] == "validation_error" and err_422["message"]

            orig_limit = rate_limiter.ip_limit_per_min
            rate_limiter.ip_limit_per_min = 2
            try:
                test_ip_headers = {"X-Forwarded-For": "198.51.100.77"}
                # 用极短非数据查询快速触发限流（前 2 次放行，第 3 次直接 429）
                for _ in range(2):
                    r_ok = client.post(
                        "/api/chat",
                        json={"question": "你好"},
                        headers=test_ip_headers,
                    )
                    assert r_ok.status_code == 200
                r_429 = client.post(
                    "/api/chat",
                    json={"question": "你好"},
                    headers=test_ip_headers,
                )
                assert r_429.status_code == 429, f"预期 429，实际: {r_429.status_code}"
                err_429 = r_429.json()
                assert err_429["error"] == "rate_limit_exceeded"
                assert "频繁" in err_429["message"]
            finally:
                rate_limiter.ip_limit_per_min = orig_limit

            print("[9/9] 422 参数校验与 429 限流校验通过")
    finally:
        # 清理测试投稿记录，保持 232 条基准数据洁净
        if inserted_test_job_id is not None:
            with db_session(DB_PATH) as conn:
                conn.execute("DELETE FROM job_intel WHERE id = ?", (inserted_test_job_id,))
                conn.commit()
        rate_limiter.reset()
        clear_headline_cache()

    print("\nALL STEP 3 API TESTS PASSED!")


if __name__ == "__main__":
    main()
