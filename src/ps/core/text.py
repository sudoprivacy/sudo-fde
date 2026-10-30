"""文本相似度。阶段一没有 embedder，用字符二元组 Jaccard 近似（中文可用）；接入 embedder 后替换。"""

from __future__ import annotations

import re

_PUNCT = re.compile(r"[\s\W_]+", re.UNICODE)


def _bigrams(text: str) -> set[str]:
    t = _PUNCT.sub("", text.lower())
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def similarity(a: str, b: str) -> float:
    x, y = _bigrams(a), _bigrams(b)
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


def containment(a: str, b: str) -> float:
    """较短一方的二元组有多少落在较长一方里。名字长短不一时比 Jaccard 稳。"""
    x, y = _bigrams(a), _bigrams(b)
    if not x or not y:
        return 0.0
    return len(x & y) / min(len(x), len(y))
