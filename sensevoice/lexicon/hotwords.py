"""读 config/hotwords.yaml → 拼成自然语言 context 字符串,喂给 Qwen3-ASR 做 biasing。

设计要点(基于之前会话的实测教训):
  - 用"中文自然语言模板"而非平铺词表。Qwen3-ASR 是 LLM-decoder ASR,
    分类提示比无差别词列表更有信息量(模型理解每组角色)。
  - 长度控制在 ~200 字符内。官方实测 context 过长(>300 字)反而会反噬
    LLM 解码器的注意力,准确率下降。
  - **不要**在 context 里提到错误形式(比如不要说"船舱应该是传参")
    —— 提到错误形式会被 LLM 当作候选,反而推高错词概率。只列正确形式。
"""

from __future__ import annotations
import os
from typing import Optional


_CACHE: dict = {}


def _load_yaml(path: str) -> Optional[dict]:
    """读 yaml 并 mtime 缓存。yaml 缺失时返回 None,调用方不注入 context。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime_ns, st.st_size)
    if _CACHE.get("key") == key:
        return _CACHE.get("data")

    try:
        import yaml  # type: ignore
    except ImportError:
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return None

    _CACHE["key"] = key
    _CACHE["data"] = data
    return data


def build_context(hotwords_yaml_path: str, max_chars: int = 200) -> str:
    """拼装 4 组分类热词成单条 context 字符串。
    yaml 不存在/为空 → 返回 "" (调用方应理解为"不注入 context")。
    """
    data = _load_yaml(hotwords_yaml_path)
    if not data or not isinstance(data, dict):
        return ""

    # 顺序: 常错词 → API → 项目 → 框架(常错词优先,即使总长被截也保住最重要的)
    order = ("common_errors", "apis", "projects", "frameworks")
    parts: list[str] = []
    for key in order:
        group = data.get(key)
        if not isinstance(group, dict):
            continue
        terms = group.get("terms") or []
        terms = [str(t).strip() for t in terms if str(t).strip()]
        if not terms:
            continue
        label = str(group.get("desc") or key).strip()
        parts.append(f"{label}: {'、'.join(terms)}")

    if not parts:
        return ""

    body = "本段对话涉及以下技术词汇——" + "; ".join(parts)
    # 超长按字符截断(中文一个字 = 一个 char,不必精算 token)
    if len(body) > max_chars:
        body = body[:max_chars]
    return body
