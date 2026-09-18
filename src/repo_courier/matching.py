from __future__ import annotations

import re

INTEREST_ALIASES = {
    "agent": ["agents", "ai agent", "ai agents", "智能体"],
    "llm": ["llms", "large language model", "large language models", "大语言模型", "大模型"],
    "mcp": ["model context protocol", "模型上下文协议"],
    "ai": ["artificial intelligence", "人工智能"],
}


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", value.lower()).strip()


def interest_terms(interest: str) -> list[str]:
    normalized = normalize(interest)
    terms = [normalized]
    terms.extend(part for part in normalized.split() if len(part) >= 2)
    terms.extend(normalize(alias) for alias in INTEREST_ALIASES.get(normalized, []))
    return list(dict.fromkeys(term for term in terms if term))


def contains(text: str, term: str) -> bool:
    if not term:
        return False
    if re.search(r"[\u4e00-\u9fff]", term):
        return term in text
    return f" {term} " in f" {text} " or term in text.split()


def match_rule(text: str, groups: list[list[str]]) -> bool:
    """Explicit groups: OR within a group, AND between groups. Never split phrases."""
    normalized = normalize(text)
    return bool(groups) and all(
        bool(group) and any(contains(normalized, normalize(alias)) for alias in group)
        for group in groups
    )


def match_rules(title: str, summary: str, body: str, rules: list[dict]) -> list[dict]:
    # Combination matches must occur in the title/summary or one local paragraph.
    windows = [title, summary, f"{title} {summary[:500]}"]
    windows.extend(p[:800] for p in re.split(r"[\n。！？!?]+", body) if p.strip())
    return [r for r in rules if any(match_rule(w, r["groups"]) for w in windows)]
