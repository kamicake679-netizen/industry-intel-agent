"""Step 0 连通性测试脚本：验证大模型接口配置、响应速度与 Token 统计。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# 确保直接运行 `python scripts/test_llm.py` 时可正确导入 app 包
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from openai import OpenAI

from app.config import settings


def main() -> int:
    api_key = settings.siliconflow_api_key.strip()
    if not api_key or api_key == "your_api_key_here":
        print(
            "[错误] 未检测到有效的 SILICONFLOW_API_KEY。\n"
            "请先复制 .env.example 为 .env 并填入真实密钥，或通过系统环境变量注入后重试。"
        )
        return 1

    print("=== Step 0 LLM 连通性测试 ===")
    print(f"Base URL : {settings.siliconflow_base_url}")
    print(f"Model    : {settings.llm_model}")

    client = OpenAI(
        api_key=api_key,
        base_url=settings.siliconflow_base_url,
    )

    start = time.perf_counter()
    try:
        response = client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {
                    "role": "system",
                    "content": "你是一个简洁的连通性测试助手，请用一句中文回复。",
                },
                {
                    "role": "user",
                    "content": "请确认你已就绪，并简短说明“行业情报简报系统连通正常”。",
                },
            ],
            max_tokens=128,
            temperature=0.2,
        )
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        print(f"[失败] 调用异常（耗时 {elapsed_ms:.1f} ms）：{exc}")
        return 1

    elapsed_ms = (time.perf_counter() - start) * 1000
    reply = (response.choices[0].message.content or "").strip()
    usage = response.usage

    print(f"回复内容 : {reply}")
    print(f"响应耗时 : {elapsed_ms:.1f} ms ({elapsed_ms / 1000:.2f} s)")
    if usage is not None:
        print(
            "Token 用量: "
            f"prompt={usage.prompt_tokens}, "
            f"completion={usage.completion_tokens}, "
            f"total={usage.total_tokens}"
        )
    else:
        print("Token 用量: 服务端未返回 usage 字段")

    print("=== 测试通过 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
