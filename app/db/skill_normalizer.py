"""技能同义词归一模块。

将各渠道 JD 与用户检索词中的技能名称统一映射为标准写法，
消除大小写差异（如 langgraph / LangGraph）与中英文别名差异（如 大模型 / LLM）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# 规范化映射表：键为全小写别名，值为标准技能名
SKILL_SYNONYMS: dict[str, str] = {
    # 大模型与核心概念
    "llm": "LLM",
    "llms": "LLM",
    "大模型": "LLM",
    "大语言模型": "LLM",
    "large language model": "LLM",
    "large language models": "LLM",
    "aigc": "AIGC",
    "生成式ai": "AIGC",
    # RAG 与知识库
    "rag": "RAG",
    "检索增强": "RAG",
    "检索增强生成": "RAG",
    "知识库问答": "RAG",
    "graphrag": "GraphRAG",
    "graph rag": "GraphRAG",
    # Agent 与编排框架
    "langgraph": "LangGraph",
    "lang graph": "LangGraph",
    "langchain": "LangChain",
    "lang chain": "LangChain",
    "llamaindex": "LlamaIndex",
    "llama index": "LlamaIndex",
    "agent": "Agent",
    "ai agent": "Agent",
    "智能体": "Agent",
    "multi-agent": "Agent",
    "multi agent": "Agent",
    "多智能体": "Agent",
    "autogen": "AutoGen",
    "crewai": "CrewAI",
    "function call": "Function Calling",
    "function calling": "Function Calling",
    "tool use": "Function Calling",
    "工具调用": "Function Calling",
    "mcp": "MCP",
    "model context protocol": "MCP",
    # 提示词工程
    "prompt": "Prompt Engineering",
    "prompt engineering": "Prompt Engineering",
    "提示词": "Prompt Engineering",
    "提示词工程": "Prompt Engineering",
    "prompt调优": "Prompt Engineering",
    # 深度学习与大模型训练/推理
    "python": "Python",
    "python3": "Python",
    "py": "Python",
    "pytorch": "PyTorch",
    "torch": "PyTorch",
    "tensorflow": "TensorFlow",
    "tf": "TensorFlow",
    "transformers": "Transformers",
    "huggingface": "Transformers",
    "hugging face": "Transformers",
    "vllm": "vLLM",
    "v-llm": "vLLM",
    "sglang": "SGLang",
    "tensorrt-llm": "TensorRT-LLM",
    "trt-llm": "TensorRT-LLM",
    "ollama": "Ollama",
    "lora": "LoRA",
    "qlora": "LoRA",
    "peft": "LoRA",
    "微调": "LoRA",
    "模型微调": "LoRA",
    "sft": "LoRA",
    "fine-tuning": "LoRA",
    "finetuning": "LoRA",
    "rlhf": "RLHF",
    "dpo": "RLHF",
    "grpo": "RLHF",
    "强化学习对齐": "RLHF",
    "deepspeed": "DeepSpeed",
    "megatron": "Megatron-LM",
    "cuda": "CUDA",
    # 数据库与检索
    "sql": "SQL",
    "text2sql": "Text-to-SQL",
    "text-to-sql": "Text-to-SQL",
    "nl2sql": "Text-to-SQL",
    "mysql": "MySQL",
    "postgresql": "PostgreSQL",
    "postgres": "PostgreSQL",
    "pg": "PostgreSQL",
    "sqlite": "SQLite",
    "redis": "Redis",
    "milvus": "Milvus",
    "elasticsearch": "Elasticsearch",
    "es": "Elasticsearch",
    "vectordb": "VectorDB",
    "vector db": "VectorDB",
    "向量数据库": "VectorDB",
    "faiss": "FAISS",
    "qdrant": "Qdrant",
    "chroma": "Chroma",
    "chromadb": "Chroma",
    # 低代码平台与主流模型生态
    "dify": "Dify",
    "coze": "Coze",
    "扣子": "Coze",
    "fastgpt": "FastGPT",
    "deepseek": "DeepSeek",
    "deep seek": "DeepSeek",
    "qwen": "Qwen",
    "通义千问": "Qwen",
    "千问": "Qwen",
    "openai": "OpenAI API",
    "openai api": "OpenAI API",
    # 算法方向与评测
    "embedding": "Embedding",
    "向量化": "Embedding",
    "bge": "Embedding",
    "rerank": "Rerank",
    "重排": "Rerank",
    "multimodal": "Multimodal",
    "多模态": "Multimodal",
    "vlm": "Multimodal",
    "nlp": "NLP",
    "自然语言处理": "NLP",
    "cv": "CV",
    "计算机视觉": "CV",
    "llm eval": "LLM Eval",
    "模型评测": "LLM Eval",
    "ragas": "LLM Eval",
    "评测": "LLM Eval",
    # 后端与工程基础设施
    "fastapi": "FastAPI",
    "fast api": "FastAPI",
    "docker": "Docker",
    "容器化": "Docker",
    "kubernetes": "Kubernetes",
    "k8s": "Kubernetes",
    "linux": "Linux",
    "ubuntu": "Linux",
    "shell": "Linux",
    "git": "Git",
    "github": "Git",
    "gitlab": "Git",
    # 通用开发语言与框架
    "c++": "C++",
    "cpp": "C++",
    "go": "Go",
    "golang": "Go",
    "java": "Java",
    "spring": "Spring Boot",
    "springboot": "Spring Boot",
    "spring boot": "Spring Boot",
    "typescript": "TypeScript",
    "ts": "TypeScript",
    "javascript": "JavaScript",
    "js": "JavaScript",
    "react": "React",
    "reactjs": "React",
    "vue": "Vue",
    "vuejs": "Vue",
    "vue3": "Vue",
}

_SPLIT_PATTERN = re.compile(r"[,，、/;；|\n]+")
_STRIP_PUNCT_PATTERN = re.compile(r"^[\s`'\"()\[\]{}<>·•\-]+|[\s`'\"()\[\]{}<>·•\-]+$")


def normalize_skill(raw: str) -> str:
    """将单个原始技能名称归一为标准写法。"""
    if not isinstance(raw, str):
        return ""
    cleaned = _STRIP_PUNCT_PATTERN.sub("", raw).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return ""

    key = cleaned.lower()
    if key in SKILL_SYNONYMS:
        return SKILL_SYNONYMS[key]

    # 对未命中映射表的纯 ASCII 英文缩写或词组做确定性大小写规范化
    if cleaned.isascii():
        if len(cleaned) <= 3 and cleaned.isalpha():
            return cleaned.upper()
        if cleaned.islower() or cleaned.isupper():
            return " ".join(
                part.upper() if len(part) <= 3 and part.isalpha() else part.capitalize()
                for part in cleaned.split(" ")
            )
    return cleaned


def normalize_skills(skills: Iterable[str] | str | None) -> list[str]:
    """对技能列表进行拆分、同义词归一并按大小写无关去重（保持原顺序）。"""
    if skills is None:
        return []

    raw_items: list[str] = []
    if isinstance(skills, str):
        raw_items.extend(_SPLIT_PATTERN.split(skills))
    else:
        for item in skills:
            if isinstance(item, str):
                raw_items.extend(_SPLIT_PATTERN.split(item))

    result: list[str] = []
    seen_lower: set[str] = set()
    for item in raw_items:
        norm = normalize_skill(item)
        if not norm:
            continue
        lower_norm = norm.lower()
        if lower_norm not in seen_lower:
            seen_lower.add(lower_norm)
            result.append(norm)
    return result
