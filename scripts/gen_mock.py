"""生成 Step 4 静态高保真稿所需的 Mock 数据（不调用 LLM）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import DB_PATH  # noqa: E402
from app.services.analytics import get_dashboard  # noqa: E402
from app.services.headline import _build_fallback_headline  # noqa: E402

CITIES = ["全部", "深圳", "广州", "东莞"]


def main() -> None:
    """导出各城市维度的看板与模板头条 Mock 数据到 web/mock/。"""
    mock_dir = ROOT_DIR / "web" / "mock"
    mock_dir.mkdir(parents=True, exist_ok=True)

    dashboards: dict[str, dict] = {}
    headlines: dict[str, dict] = {}

    for city_key in CITIES:
        city_arg = None if city_key == "全部" else city_key
        data = get_dashboard(city=city_arg, db_path=DB_PATH)

        # 确保“全部”视图的 latest 至少有一条带 user_submit 标记，用于静态稿展示朱砂“新”字标
        if city_key == "全部" and data.get("latest"):
            has_submit = any(item.get("source") == "user_submit" for item in data["latest"])
            if not has_submit:
                data["latest"][0]["source"] = "user_submit"

        dashboards[city_key] = data
        headlines[city_key] = _build_fallback_headline(data, city_arg)

    dash_path = mock_dir / "dashboard.json"
    head_path = mock_dir / "headline.json"

    dash_path.write_text(
        json.dumps(dashboards, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    head_path.write_text(
        json.dumps(headlines, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    print(f"已生成: {dash_path} ({dash_path.stat().st_size} bytes)")
    print(f"已生成: {head_path} ({head_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
