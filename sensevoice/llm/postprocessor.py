# sensevoice/llm/postprocessor.py
# LLM-based ASR post-processor: sends recognised text to an OpenAI-compatible
# chat endpoint for typo correction / light polish, with circuit-breaker,
# fallback model, response caching, and guardrails against hallucination.

import collections
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import List, Optional, Tuple


class LLMPostProcessor:
    PROMPTS = {
        "correct": (
            "你是ASR后处理器。只做同音字/错别字/标点修正。"
            "不要扩写、不要解释、不要新增事实。"
            "代码、路径、命令、参数、英文术语必须保持原样。"
            "若无法确定修改点，请原样返回输入文本。"
            "只返回最终文本。"
        ),
        "polish_light": (
            "你是ASR后处理器。先做同音字/错别字/标点修正，"
            "并允许轻度润色语义（补连接词、断句、轻微书面化），"
            "但不得新增事实或改变用户意图。"
            "禁止把问句改写成解释性回答。"
            "禁止输出“意思是/是指/指的是”等释义句式。"
            "代码、路径、命令、参数、英文术语必须保持原样。"
            "若无法确定修改点，请原样返回输入文本。"
            "不要使用Markdown格式或反引号。"
            "只返回最终文本。"
        ),
        "polish_coding": (
            "你是面向中文编程口述场景的ASR后处理器。"
            "优先积极修正明显同音字、口误、断句、标点和口语残片，"
            "并允许适度重组语序，让句子更自然、更像真实提问或指令。"
            "优先修正真正影响语义的内容词、技术术语、名词短语、命令词。"
            "不要为了书面化去改动的/地/得、吗/呢/啊、了/着/过这类低价值虚词；"
            "除非语法错误极其明确，否则保持原样。"
            "如果一句里同时存在术语错误和虚词歧义，只修术语错误，不做无关紧要的字面替换。"
            "但不得新增事实、不得改变用户意图、不得把问题改成答案。"
            "代码、路径、命令、参数、英文术语、数字必须保持原样。"
            "禁止输出解释、释义、总结、Markdown、反引号。"
            "若不能确定，就保留原文局部。"
            "只返回最终文本。"
        ),
        "polish_coding_aggressive": (
            "你是中文编程口述场景的ASR后处理器。"
            "用户正在通过语音与编程助手交流，话题涉及代码开发、编译构建、调试、架构设计、技术讨论等软件工程场景。"
            "\n处理步骤："
            "1. 通读全句，主动识别在编程语境下不通顺、不合理的词语或片段；"
            "2. 对每个不通顺之处，根据发音相似性和编程语境推断说话者的原本意图"
            '（如"传舱"在编程语境下很可能是"传参"，"保护红"很可能是"保护宏"，"走网"很可能是"组网/整网"）；'
            "3. 修正同音字、近音字、错别字、口误、重复片段、错误断句和标点；"
            "4. 允许适度重组语序，让结果更像清晰自然的技术沟通文本。"
            "\n约束："
            "不得新增事实、不得编造未说出的内容、不得把问题改成答案；"
            "代码、路径、命令、参数、英文术语、数字保持原样；"
            "不要做的/地/得、吗/呢/啊等低价值虚词替换；"
            "禁止输出解释、释义、Markdown、反引号；"
            "只返回最终文本。"
        ),
    }

    def __init__(
        self,
        enabled: bool,
        base_url: str,
        api_key: str,
        model: str,
        fallback_model: str,
        mode: str,
        timeout_ms: int,
        max_tokens: int,
        temperature: float,
        circuit_max_fails: int,
        circuit_cooldown_sec: int,
        hard_cooldown_sec: int,
        retry_on_timeout: bool,
        retry_backoff_ms: int,
        model_auto: bool,
        model_probe_timeout_ms: int,
        min_chars: int,
        cache_ttl_sec: int,
        cache_max_entries: int,
        dynamic_max_tokens: bool,
        output_token_factor: float,
    ):
        self.requested = bool(enabled)
        self.enabled = False
        self.reason = "off"
        self.last_error = ""
        self.timeout_sec = max(0.2, float(timeout_ms) / 1000.0)
        self.max_tokens = max(32, int(max_tokens))
        self.temperature = float(temperature)
        self.circuit_max_fails = max(1, int(circuit_max_fails))
        self.circuit_cooldown_sec = max(5, int(circuit_cooldown_sec))
        self.hard_cooldown_sec = max(self.circuit_cooldown_sec, int(hard_cooldown_sec))
        self.retry_on_timeout = bool(retry_on_timeout)
        self.retry_backoff_sec = max(0.0, float(retry_backoff_ms) / 1000.0)
        self._fail_streak = 0
        self._circuit_open_until = 0.0
        self.model_auto = bool(model_auto)
        self.model_probe_timeout_sec = max(0.2, float(model_probe_timeout_ms) / 1000.0)
        self.min_chars = max(1, int(min_chars))
        self.cache_ttl_sec = max(0, int(cache_ttl_sec))
        self.cache_max_entries = max(0, int(cache_max_entries))
        self.dynamic_max_tokens = bool(dynamic_max_tokens)
        self.output_token_factor = max(0.2, min(2.0, float(output_token_factor)))
        self._cache: "collections.OrderedDict[str, Tuple[float, str]]" = collections.OrderedDict()
        self._model_ids: List[str] = []
        self.model = (model or "").strip()
        self.initial_model = self.model
        self.fallback_model = (fallback_model or "").strip()
        mode_norm = (mode or "").strip().lower()
        self.mode = mode_norm if mode_norm in self.PROMPTS else "correct"
        # Guardrails vary by rewrite mode.
        if self.mode == "polish_light":
            self.min_keep_ratio = 0.60
            self.max_expand_ratio = 1.50
        elif self.mode == "polish_coding":
            self.min_keep_ratio = 0.45
            self.max_expand_ratio = 1.80
        elif self.mode == "polish_coding_aggressive":
            self.min_keep_ratio = 0.35
            self.max_expand_ratio = 2.10
        else:
            self.min_keep_ratio = 0.50
            self.max_expand_ratio = 2.20
        self.url = ""
        self.models_url = ""
        self.api_key = (api_key or "").strip()

        if not self.requested:
            return
        if not base_url.strip():
            self.reason = "base_url_missing"
            return
        if not self.api_key:
            self.reason = "api_key_missing"
            return

        self.url = self._normalize_endpoint(base_url)
        self.models_url = self._normalize_models_endpoint(base_url)
        if self.model_auto:
            self._autoselect_models()
        if not self.model:
            self.reason = "model_missing"
            return
        self.enabled = True
        self.reason = f"ready:model={self.model},fallback={self.fallback_model or '-'}"
        self._warmup_probe()

    def _note_success(self) -> None:
        self._fail_streak = 0
        self._circuit_open_until = 0.0
        self.last_error = ""

    def _note_failure(self, reason: str) -> None:
        hard_tags = ("model_not_found", "http_auth", "api_key_missing", "base_url_missing")
        if any(tag in reason for tag in hard_tags):
            self._fail_streak = self.circuit_max_fails
            self._circuit_open_until = time.time() + self.hard_cooldown_sec
            self.last_error = f"{reason};hard_circuit_open:{self.hard_cooldown_sec}s"
            return
        self._fail_streak += 1
        self.last_error = reason
        if self._fail_streak >= self.circuit_max_fails:
            self._circuit_open_until = time.time() + self.circuit_cooldown_sec
            self.last_error = f"{reason};circuit_open:{self.circuit_cooldown_sec}s"

    @staticmethod
    def _normalize_endpoint(base_url: str) -> str:
        base = base_url.strip().rstrip("/")
        if base.endswith("/chat/completions") or base.endswith("/completions"):
            return base
        return f"{base}/chat/completions"

    @staticmethod
    def _normalize_models_endpoint(base_url: str) -> str:
        base = base_url.strip().rstrip("/")
        for suffix in ("/chat/completions", "/completions"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        return f"{base}/models"

    @staticmethod
    def _model_eq(a: str, b: str) -> bool:
        return a.strip().lower() == b.strip().lower()

    @staticmethod
    def _extract_model_ids(obj: dict) -> List[str]:
        data = obj.get("data")
        out: List[str] = []
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                mid = item.get("id")
                if isinstance(mid, str) and mid.strip():
                    out.append(mid.strip())
        return out

    def _fetch_model_ids(self) -> Tuple[List[str], str]:
        req = urllib.request.Request(
            self.models_url,
            method="GET",
            headers={
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.model_probe_timeout_sec) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            obj = json.loads(raw)
            mids = self._extract_model_ids(obj)
            if not mids:
                return [], "models_empty"
            return mids, ""
        except urllib.error.HTTPError as e:
            return [], f"models_http_{e.code}"
        except Exception as e:
            return [], f"models_err:{type(e).__name__}"

    def _pick_preferred_model(self, model_ids: List[str]) -> str:
        if not model_ids:
            return ""
        # 1) keep user configured primary/fallback if available
        for pref in (self.model, self.fallback_model):
            if not pref:
                continue
            for mid in model_ids:
                if self._model_eq(pref, mid):
                    return mid
        # 2) keyword preference order for coding post-processing
        keywords = ("deepseek", "qwen", "gpt", "claude")
        for kw in keywords:
            for mid in model_ids:
                if kw in mid.lower():
                    return mid
        # 3) fallback to first available
        return model_ids[0]

    def _pick_fallback_model(self, model_ids: List[str], primary: str) -> str:
        if not model_ids:
            return ""
        for pref in (self.fallback_model, self.initial_model):
            if not pref:
                continue
            for mid in model_ids:
                if self._model_eq(pref, mid) and not self._model_eq(mid, primary):
                    return mid
        for mid in model_ids:
            if not self._model_eq(mid, primary):
                return mid
        return ""

    def _autoselect_models(self) -> bool:
        mids, err = self._fetch_model_ids()
        if err:
            # Keep configured model path when probing fails.
            if self.model:
                self.last_error = err
                return False
            self.reason = err
            return False
        self._model_ids = mids
        primary = self._pick_preferred_model(mids)
        if not primary:
            self.reason = "model_unavailable"
            return False
        self.model = primary
        if not self.fallback_model or self._model_eq(self.fallback_model, self.model):
            self.fallback_model = self._pick_fallback_model(mids, self.model)
        return True

    def _cache_get(self, key: str) -> str:
        if self.cache_max_entries <= 0 or self.cache_ttl_sec <= 0:
            return ""
        row = self._cache.get(key)
        if row is None:
            return ""
        ts, out = row
        if time.time() - ts > self.cache_ttl_sec:
            self._cache.pop(key, None)
            return ""
        # refresh LRU order
        self._cache.move_to_end(key)
        return out

    def _cache_put(self, key: str, out: str) -> None:
        if self.cache_max_entries <= 0 or self.cache_ttl_sec <= 0:
            return
        self._cache[key] = (time.time(), out)
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_max_entries:
            self._cache.popitem(last=False)

    def _choose_max_tokens(self, src: str) -> int:
        if not self.dynamic_max_tokens:
            return self.max_tokens
        n = len((src or "").strip())
        # Single-turn rewrite rarely needs long output; shrink response budget
        # to reduce latency and over-expansion risk.
        dyn = int(round(n * self.output_token_factor)) + 8
        dyn = max(16, dyn)
        return min(self.max_tokens, dyn)

    def _warmup_probe(self) -> None:
        if not self.enabled:
            return
        probe_src = "请修正这一句中的错别字。"
        prompt = self._system_prompt("mid", [])
        out, err = self._request_once(self.model, probe_src, prompt, self._choose_max_tokens(probe_src))
        if out:
            self._note_success()
            return
        if self.fallback_model and not self._model_eq(self.fallback_model, self.model):
            out2, err2 = self._request_once(
                self.fallback_model,
                probe_src,
                prompt,
                self._choose_max_tokens(probe_src),
            )
            if out2:
                self.model = self.fallback_model
                self._note_success()
                self.reason = f"ready:model={self.model},fallback=-"
                return
            err = f"primary:{err or 'failed'};fallback:{err2 or 'failed'}"
        self._note_failure(err or "warmup_failed")
        if "hard_circuit_open" in self.last_error:
            self.reason = f"degraded:{self.last_error}"

    @staticmethod
    def _extract_text(obj: dict) -> str:
        choices = obj.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str):
                        return content
                    if isinstance(content, list):
                        out_parts: List[str] = []
                        for item in content:
                            if isinstance(item, dict):
                                t = item.get("text")
                                if isinstance(t, str) and t:
                                    out_parts.append(t)
                        if out_parts:
                            return "".join(out_parts)
                text = first.get("text")
                if isinstance(text, str):
                    return text
        output_text = obj.get("output_text")
        if isinstance(output_text, str):
            return output_text
        return ""

    def _system_prompt(
        self,
        route: str = "mid",
        glossary: Optional[List[str]] = None,
        focus_tokens: Optional[List[str]] = None,
    ) -> str:
        base = self.PROMPTS.get(self.mode, self.PROMPTS["correct"])
        if route == "low":
            if self.mode == "polish_coding_aggressive":
                base += " 当前语句置信度较低，请尽可能利用上下文主动修正明显错误和不自然表达，但仍不得新增事实。"
            else:
                base += " 当前语句置信度较低，请更积极地修正明显同音误识别，但仍不得新增事实。"
        elif route == "high":
            if self.mode in {"polish_coding", "polish_coding_aggressive"}:
                if self.mode == "polish_coding_aggressive":
                    base += " 当前语句置信度较高，但若存在明显术语错误、命令词错误、错词或不自然表达，仍应主动修正；不要做的/地/得之类低价值虚词替换。"
                else:
                    base += " 当前语句置信度较高，但仍允许修正明显术语错误、口语误识别和断句问题；不要做低价值虚词替换，也不要过度改写。"
            else:
                base += " 当前语句置信度高，尽量少改，仅修正明显错误。"
        if glossary:
            joined = ", ".join(glossary[:24])
            if joined:
                base += (
                    " 优先参考以下项目术语，仅在语义明显匹配时替换："
                    f"{joined}。"
                    " 术语不确定时保留原样。"
                )
        if focus_tokens:
            joined_focus = ", ".join([t for t in focus_tokens if t][:6])
            if joined_focus:
                base += (
                    " 当前低置信重点词片段："
                    f"{joined_focus}。"
                    " 优先检查这些词及其紧邻上下文，只在必要范围内局部修正，避免整句无关改写。"
                )
        return base

    def _request_once(self, model_name: str, src: str, prompt: str, req_max_tokens: int) -> Tuple[str, str]:
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": src},
            ],
            "temperature": self.temperature,
            "max_tokens": req_max_tokens,
            "stream": False,
            # Best-effort flags for providers that support disabling reasoning mode.
            "thinking": False,
            "enable_thinking": False,
            "reasoning_effort": "low",
            "chat_template_kwargs": {"thinking": False},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            obj = json.loads(raw)
            out = self._extract_text(obj).strip()
            if not out:
                return "", "empty_output"
            return out, ""
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception as read_exc:
                print(f"[sensevoice] warning: failed to read HTTP error body: {read_exc}", file=sys.stderr)
                body = ""
            lower = body.lower()
            if e.code in (401, 403):
                return "", f"http_auth:{e.code}"
            if e.code == 429:
                return "", "http_rate_limited"
            if (
                e.code in (400, 404)
                and (
                    "notfound" in lower
                    or "not found" in lower
                    or "does not exist" in lower
                    or ("invalid" in lower and "model" in lower)
                )
            ):
                return "", "model_not_found"
            return "", f"http_error:{e.code}"
        except urllib.error.URLError as e:
            reason = str(getattr(e, "reason", e)).lower()
            if "timed out" in reason:
                return "", "timeout"
            return "", f"url_error:{type(e).__name__}"
        except Exception as e:
            if "timeout" in type(e).__name__.lower():
                return "", "timeout"
            return "", f"err:{type(e).__name__}"

    def process(
        self,
        text: str,
        route: str = "mid",
        glossary: Optional[List[str]] = None,
        focus_tokens: Optional[List[str]] = None,
    ) -> str:
        if not self.enabled:
            return text
        src = (text or "").strip()
        if not src:
            return text
        if len(src) < self.min_chars:
            return text
        route_tag = route if route in ("high", "mid", "low") else "mid"
        if route_tag == "high" and self.mode not in {"polish_coding", "polish_coding_aggressive"}:
            self.last_error = "skip_high_conf"
            return text

        prompt = self._system_prompt(route_tag, glossary, focus_tokens)
        req_max_tokens = self._choose_max_tokens(src)
        if route_tag == "low":
            req_max_tokens = min(self.max_tokens, int(req_max_tokens * 1.5) + 12)

        glossary_key = ",".join((glossary or [])[:8])
        focus_key = ",".join((focus_tokens or [])[:6])
        cache_key = f"{route_tag}|g={glossary_key}|f={focus_key}|{src}"
        cached = self._cache_get(cache_key)
        if cached:
            self.last_error = ""
            return cached
        now = time.time()
        if now < self._circuit_open_until:
            remain = int(max(1, self._circuit_open_until - now))
            self.last_error = f"circuit_open:{remain}s"
            return text

        out, err = self._request_once(self.model, src, prompt, req_max_tokens)
        if not out and err == "timeout" and self.retry_on_timeout:
            if self.retry_backoff_sec > 0:
                time.sleep(self.retry_backoff_sec)
            out_retry_to, err_retry_to = self._request_once(self.model, src, prompt, req_max_tokens)
            if out_retry_to:
                self._note_success()
                self._cache_put(cache_key, out_retry_to)
                return out_retry_to
            err = f"timeout_retry:{err_retry_to or 'failed'}"
        if out:
            self._note_success()
            self._cache_put(cache_key, out)
            return out
        if err == "model_not_found" and self.model_auto and self._autoselect_models():
            out_retry, err_retry = self._request_once(self.model, src, prompt, req_max_tokens)
            if out_retry:
                self._note_success()
                self._cache_put(cache_key, out_retry)
                return out_retry
            err = f"{err};reprobe:{err_retry or 'failed'}"

        # Some providers/models return reasoning-only responses for thinking models.
        # Fallback model keeps the chain usable for low-latency correction.
        if self.fallback_model and self.fallback_model != self.model:
            out2, err2 = self._request_once(self.fallback_model, src, prompt, req_max_tokens)
            if out2:
                self._note_success()
                self._cache_put(cache_key, out2)
                return out2
            self._note_failure(f"primary:{err or 'failed'};fallback:{err2 or 'failed'}")
            return text

        self._note_failure(err or "failed")
        return text
