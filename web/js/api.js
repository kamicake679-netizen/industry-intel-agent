/**
 * 真实后端 API 封装：看板、头条、SSE 流式问答（支持 AbortController）、JD 抽取与简报推送。
 * 统一处理 HTTP 429 限流（联动顶部「更正启事」提示条）与网络异常。
 */

export class ApiError extends Error {
  /**
   * @param {string} message
   * @param {number} status
   * @param {string} [errorCode]
   * @param {number|null} [retryAfter]
   */
  constructor(message, status = 0, errorCode = "api_error", retryAfter = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.errorCode = errorCode;
    this.retryAfter = retryAfter;
  }
}

let noticeHandler = null;

/**
 * 注册全局接口提示回调（用于 429 限流/额度提示）。
 * @param {(payload: { tag: string, message: string }) => void} fn
 */
export function setApiNoticeHandler(fn) {
  noticeHandler = typeof fn === "function" ? fn : null;
}

function emitRateLimitNotice(message) {
  if (noticeHandler) {
    noticeHandler({
      tag: "更正启事",
      message: message || "今日演示额度已用完或请求过于频繁，请稍后再试；看板数据仍可正常浏览。"
    });
  }
}

async function parseErrorResponse(res) {
  const retryHeader = res.headers.get("Retry-After");
  const retryAfter = retryHeader ? Number(retryHeader) || null : null;
  let errorCode = `http_${res.status}`;
  let message = `请求失败（HTTP ${res.status}）`;

  try {
    const body = await res.json();
    if (body && typeof body === "object") {
      if (body.error) errorCode = String(body.error);
      if (body.message) message = String(body.message);
      else if (body.detail) message = String(body.detail);
    }
  } catch {
    // 忽略非 JSON 错误体
  }

  if (res.status === 429) {
    const rateMsg = `今日演示额度已用完 / ${message}`;
    emitRateLimitNotice(rateMsg);
    return new ApiError(rateMsg, 429, errorCode, retryAfter);
  }
  return new ApiError(message, res.status, errorCode, retryAfter);
}

async function requestJson(url, options = {}) {
  let res;
  try {
    res = await fetch(url, {
      headers: {
        Accept: "application/json",
        ...(options.headers || {})
      },
      ...options
    });
  } catch (err) {
    if (err && err.name === "AbortError") {
      throw err;
    }
    throw new ApiError("网络连接异常，请检查后端服务是否正常运行。", 0, "network_error");
  }

  if (!res.ok) {
    throw await parseErrorResponse(res);
  }
  return res.json();
}

/**
 * 获取服务健康状态与模型/预算信息。
 */
export async function getHealth() {
  return requestJson("/api/health");
}

/**
 * 获取指定城市的看板聚合数据。
 * @param {string} city
 */
export async function getDashboard(city = "全部") {
  const qs = city && city !== "全部" ? `?city=${encodeURIComponent(city)}` : "";
  return requestJson(`/api/dashboard${qs}`);
}

/**
 * 获取指定城市的头条数据。
 * @param {string} city
 * @param {{ refresh?: boolean }} [opts]
 */
export async function getHeadline(city = "全部", opts = {}) {
  const params = new URLSearchParams();
  if (city && city !== "全部") {
    params.set("city", city);
  }
  if (opts.refresh) {
    params.set("refresh", "true");
  }
  const qs = params.toString();
  return requestJson(`/api/headline${qs ? `?${qs}` : ""}`);
}

/**
 * 发起 SSE 流式问答：使用 fetch + ReadableStream 解析 event/data 帧。
 * @param {string} question
 * @param {Array<{ role: string, content: string }>} history
 * @param {(eventName: string, data: any) => Promise<void> | void} onEvent
 * @param {AbortSignal} [signal]
 */
export async function chatStream(question, history = [], onEvent, signal) {
  let res;
  try {
    res = await fetch("/api/chat/stream", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream"
      },
      body: JSON.stringify({
        question,
        history: Array.isArray(history) ? history : []
      }),
      signal
    });
  } catch (err) {
    if (err && err.name === "AbortError") {
      throw err;
    }
    throw new ApiError("问询台连接失败，请确认后端服务已启动。", 0, "network_error");
  }

  if (!res.ok) {
    throw await parseErrorResponse(res);
  }

  if (!res.body) {
    throw new ApiError("当前浏览器不支持流式响应读取。", 0, "stream_unsupported");
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  const processFrame = async (rawFrame) => {
    const lines = rawFrame.split("\n");
    let eventName = "message";
    const dataLines = [];

    for (const line of lines) {
      if (!line || line.startsWith(":")) {
        continue;
      }
      if (line.startsWith("event:")) {
        eventName = line.slice(6).trim() || "message";
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trimStart());
      }
    }

    if (!dataLines.length) {
      return;
    }

    const rawData = dataLines.join("\n").trim();
    if (!rawData) {
      return;
    }

    let parsed = rawData;
    try {
      parsed = JSON.parse(rawData);
    } catch {
      // 保留原始文本
    }
    await onEvent(eventName, parsed);
  };

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) {
        break;
      }
      const chunk = decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      buffer += chunk;

      let sepIndex = buffer.indexOf("\n\n");
      while (sepIndex !== -1) {
        const frame = buffer.slice(0, sepIndex);
        buffer = buffer.slice(sepIndex + 2);
        if (frame.trim()) {
          await processFrame(frame);
        }
        sepIndex = buffer.indexOf("\n\n");
      }
    }

    // 处理解码器残留与末尾未带双换行的最后一帧
    const tail = decoder.decode().replace(/\r\n/g, "\n");
    buffer += tail;
    if (buffer.trim()) {
      await processFrame(buffer.trim());
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      // 忽略已释放的锁
    }
  }
}

/**
 * 提交原始 JD 文本进行结构化抽取并入库。
 * @param {string} rawText
 */
export async function extractJob(rawText) {
  return requestJson("/api/extract", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ raw_text: rawText })
  });
}

/**
 * 生成今日简报并可选推送 Webhook。
 * @param {{ city?: string, webhookUrl?: string, push?: boolean }} [opts]
 */
export async function generateBrief({ city = "全部", webhookUrl = "", push = false } = {}) {
  return requestJson("/api/brief", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      city: city && city !== "全部" ? city : null,
      webhook_url: webhookUrl && webhookUrl.trim() ? webhookUrl.trim() : null,
      push: Boolean(push)
    })
  });
}
