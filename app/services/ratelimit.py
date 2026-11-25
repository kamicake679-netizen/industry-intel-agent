"""请求限流与全局每日 LLM 调用预算管理模块。"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import date
from typing import Any

from fastapi import Request

from app.config import settings


class RateLimitError(Exception):
    """限流或每日预算耗尽异常。"""

    def __init__(self, error_code: str, message: str, retry_after: int = 60) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.retry_after = retry_after


class RateLimiter:
    """线程安全的单 IP 滑动窗口限流器 + 全局每日 LLM 调用预算器。"""

    def __init__(self, ip_limit_per_min: int = 10, window_seconds: int = 60) -> None:
        self.ip_limit_per_min = ip_limit_per_min
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._ip_windows: dict[str, deque[float]] = {}
        self._budget_date: str = date.today().isoformat()
        self._daily_used: int = 0

    def _roll_day_if_needed(self) -> None:
        today_str = date.today().isoformat()
        if today_str != self._budget_date:
            self._budget_date = today_str
            self._daily_used = 0

    def check_and_consume(self, client_ip: str, cost: int = 1) -> None:
        """校验单 IP 频次与全局每日预算，通过则计入消耗，否则抛出 RateLimitError。"""
        ip_key = (client_ip or "unknown").strip() or "unknown"
        now = time.monotonic()
        cutoff = now - self.window_seconds

        with self._lock:
            dq = self._ip_windows.setdefault(ip_key, deque())
            while dq and dq[0] <= cutoff:
                dq.popleft()

            if len(dq) >= self.ip_limit_per_min:
                retry_after = max(1, int(self.window_seconds - (now - dq[0])) + 1)
                raise RateLimitError(
                    error_code="rate_limit_exceeded",
                    message=f"请求过于频繁（单 IP 每分钟上限 {self.ip_limit_per_min} 次），请 {retry_after} 秒后再试。",
                    retry_after=retry_after,
                )

            self._roll_day_if_needed()
            daily_limit = int(settings.daily_llm_budget)
            if self._daily_used + cost > daily_limit:
                raise RateLimitError(
                    error_code="daily_budget_exceeded",
                    message=f"今日演示额度（{daily_limit} 次）已用完，请明日再试，当前仍可浏览只读看板数据。",
                    retry_after=3600,
                )

            dq.append(now)
            self._daily_used += cost

    def try_consume_budget(self, cost: int = 1) -> bool:
        """仅检查并消耗全局每日预算（供头条等后台能力使用，超限返回 False 以便优雅降级）。"""
        with self._lock:
            self._roll_day_if_needed()
            daily_limit = int(settings.daily_llm_budget)
            if self._daily_used + cost > daily_limit:
                return False
            self._daily_used += cost
            return True

    def get_budget_status(self) -> dict[str, Any]:
        """返回当前每日预算与限流配置状态。"""
        with self._lock:
            self._roll_day_if_needed()
            daily_limit = int(settings.daily_llm_budget)
            remaining = max(0, daily_limit - self._daily_used)
            return {
                "date": self._budget_date,
                "daily_limit": daily_limit,
                "daily_used": self._daily_used,
                "daily_remaining": remaining,
                "ip_limit_per_min": self.ip_limit_per_min,
                "window_seconds": self.window_seconds,
            }

    def reset(self) -> None:
        """清空限流窗口与当日计数（供自动化测试复用）。"""
        with self._lock:
            self._ip_windows.clear()
            self._budget_date = date.today().isoformat()
            self._daily_used = 0


rate_limiter = RateLimiter(ip_limit_per_min=10, window_seconds=60)


def get_client_ip(request: Request) -> str:
    """提取客户端真实 IP（兼容反向代理 X-Forwarded-For）。"""
    forwarded = request.headers.get("x-forwarded-for", "").strip()
    if forwarded:
        first_ip = forwarded.split(",")[0].strip()
        if first_ip:
            return first_ip
    if request.client and request.client.host:
        return str(request.client.host)
    return "127.0.0.1"
