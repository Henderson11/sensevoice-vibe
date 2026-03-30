# sensevoice/lexicon/project.py
# Project lexicon: scans a project directory for identifiers and technical
# terms, then provides fuzzy-match hints and normalisation for ASR output
# to improve domain-specific recognition accuracy.

import collections
import difflib
import os
import re
import sys
from typing import Dict, List, Tuple

from sensevoice.text.constants import COMMON_TECH_STOPWORDS, IDENT_RE, TECH_TOKEN_RE


class ProjectLexicon:
    def __init__(
        self,
        enabled: bool,
        project_root: str,
        max_terms: int,
        hint_limit: int,
        min_term_len: int,
        extra_terms_file: str,
    ):
        self.requested = bool(enabled)
        self.enabled = False
        self.reason = "disabled"
        self.project_root = ""
        self.max_terms = max(200, int(max_terms))
        self.hint_limit = max(0, int(hint_limit))
        self.min_term_len = max(2, int(min_term_len))
        self.extra_terms_file = os.path.expanduser((extra_terms_file or "").strip())
        self.terms: List[str] = []
        self._terms_lower: set[str] = set()
        self._terms_by_initial: Dict[str, List[str]] = {}

        if not self.requested:
            return
        root = os.path.abspath(os.path.expanduser((project_root or "").strip() or os.getcwd()))
        if not os.path.isdir(root):
            self.reason = f"root_missing:{root}"
            return
        self.project_root = root

        counter: collections.Counter[str] = collections.Counter()
        self._collect_from_paths(counter)
        self._collect_from_files(counter)
        self._collect_from_extra_file(counter)

        if not counter:
            self.reason = "empty_lexicon"
            return

        ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[: self.max_terms]
        self.terms = [k for k, _ in ranked]
        self._terms_lower = {t.lower() for t in self.terms}
        for t in self.terms:
            key = t[0].lower()
            self._terms_by_initial.setdefault(key, []).append(t)
        self.enabled = True
        self.reason = f"ready:terms={len(self.terms)}"

    @staticmethod
    def _is_source_file(path: str) -> bool:
        lower = path.lower()
        exts = (
            ".py",
            ".js",
            ".ts",
            ".tsx",
            ".jsx",
            ".go",
            ".rs",
            ".java",
            ".kt",
            ".cpp",
            ".cc",
            ".c",
            ".h",
            ".hpp",
            ".sh",
            ".yaml",
            ".yml",
            ".json",
            ".toml",
            ".md",
            ".txt",
            ".ini",
            ".cfg",
        )
        return lower.endswith(exts)

    def _collect_term(self, counter: collections.Counter[str], token: str, weight: int = 1) -> None:
        t = (token or "").strip("_- ")
        if len(t) < self.min_term_len:
            return
        if not IDENT_RE.fullmatch(t):
            return
        if t.isdigit():
            return
        if t.lower() in COMMON_TECH_STOPWORDS:
            return
        counter[t] += weight

    def _collect_from_paths(self, counter: collections.Counter[str]) -> None:
        ignore_dirs = {
            ".git",
            ".venv",
            ".mypy_cache",
            "__pycache__",
            "node_modules",
            ".idea",
            ".vscode",
            "dist",
            "build",
            ".cache",
            ".trash",
        }
        max_files = 1800
        seen_files = 0
        for cur, dirs, files in os.walk(self.project_root):
            dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith(".")]
            rel = os.path.relpath(cur, self.project_root)
            for tok in IDENT_RE.findall(rel):
                self._collect_term(counter, tok, weight=2)
            for fn in files:
                seen_files += 1
                if seen_files > max_files:
                    return
                base, _ = os.path.splitext(fn)
                for tok in IDENT_RE.findall(base):
                    self._collect_term(counter, tok, weight=2)

    def _collect_from_files(self, counter: collections.Counter[str]) -> None:
        ignore_dirs = {
            ".git",
            ".venv",
            ".mypy_cache",
            "__pycache__",
            "node_modules",
            ".idea",
            ".vscode",
            "dist",
            "build",
            ".cache",
            ".trash",
        }
        max_files = 260
        max_bytes = 128 * 1024
        scanned = 0
        for cur, dirs, files in os.walk(self.project_root):
            dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith(".")]
            for fn in files:
                if scanned >= max_files:
                    return
                fp = os.path.join(cur, fn)
                if not self._is_source_file(fp):
                    continue
                try:
                    sz = os.path.getsize(fp)
                    if sz > max_bytes:
                        continue
                    with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                        raw = f.read(max_bytes)
                except Exception as exc:
                    print(f"[sensevoice] warning: failed to read {fp}: {exc}", file=sys.stderr)
                    continue
                scanned += 1
                for tok in IDENT_RE.findall(raw):
                    self._collect_term(counter, tok, weight=1)

    def _collect_from_extra_file(self, counter: collections.Counter[str]) -> None:
        if not self.extra_terms_file:
            return
        if not os.path.isfile(self.extra_terms_file):
            return
        try:
            with open(self.extra_terms_file, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    t = line.strip()
                    if not t or t.startswith("#"):
                        continue
                    for tok in IDENT_RE.findall(t):
                        self._collect_term(counter, tok, weight=3)
        except Exception as exc:
            print(f"[sensevoice] warning: failed to read extra terms file {self.extra_terms_file}: {exc}", file=sys.stderr)
            return

    def contains(self, token: str) -> bool:
        return token.lower() in self._terms_lower

    def hints_for_text(self, text: str) -> List[str]:
        if not self.enabled or self.hint_limit <= 0:
            return []
        src = (text or "").strip()
        if not src:
            return []
        hints: List[str] = []
        seen = set()
        raw = TECH_TOKEN_RE.findall(src)
        for tok in raw:
            t = tok.strip()
            if len(t) < self.min_term_len:
                continue
            low = t.lower()
            if low in self._terms_lower:
                if low not in seen:
                    hints.append(t)
                    seen.add(low)
                continue
            bucket = self._terms_by_initial.get(low[0], self.terms)
            best = difflib.get_close_matches(t, bucket, n=1, cutoff=0.82)
            if not best:
                continue
            b = best[0]
            bl = b.lower()
            if bl in seen:
                continue
            hints.append(b)
            seen.add(bl)
            if len(hints) >= self.hint_limit:
                break
        return hints

    def normalize_text(self, text: str, cutoff: float = 0.90, max_rewrites: int = 4) -> str:
        if not self.enabled:
            return text
        src = text or ""
        if not src.strip():
            return src

        rewrites = 0
        out_parts: List[str] = []
        last = 0
        for m in TECH_TOKEN_RE.finditer(src):
            out_parts.append(src[last : m.start()])
            tok = m.group(0)
            low = tok.lower()
            new_tok = tok
            if low not in self._terms_lower and rewrites < max_rewrites:
                bucket = self._terms_by_initial.get(low[0], self.terms)
                best = difflib.get_close_matches(tok, bucket, n=1, cutoff=cutoff)
                if best:
                    cand = best[0]
                    ratio = difflib.SequenceMatcher(None, tok.lower(), cand.lower()).ratio()
                    if ratio >= cutoff and abs(len(tok) - len(cand)) <= 3:
                        new_tok = cand
                        rewrites += 1
            out_parts.append(new_tok)
            last = m.end()
        out_parts.append(src[last:])
        return "".join(out_parts)
