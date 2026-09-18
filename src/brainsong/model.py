from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def canonical(url: str) -> str:
    p = urlsplit(url.strip())
    if p.scheme not in {"https", "http"} or not p.hostname or p.username or p.password:
        return ""
    # Only known tracking parameters; article identifiers are preserved.
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid", "spm"}]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", urlencode(q), ""))


def title_key(title: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", title.casefold())


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def dictionary_result(url: str) -> bool:
    host = (urlsplit(url).hostname or '').lower()
    domains = {'merriam-webster.com', 'merriam-webstercollegiate.com', 'm-w.com',
               'englishreference.com', 'vocabulary.com', 'definder.net',
               'collinsdictionary.com', 'oed.com', 'wordnet-online.com',
               'yourdictionary.com', 'dictionary.com', 'wordreference.com',
               'quword.com', 'wordsdefined.com', 'dictai.org', 'eudic.net',
               'qimingbaike.com', 'name-doctor.com', 'behindthename.com'}
    return any(host == d or host.endswith('.' + d) for d in domains)


def freshness_factor(item, today: date) -> float:
    """Calendar-day windows include today: days 0–2, 3–6, 7–29."""
    try:
        age = (today - date.fromisoformat(item.published[:10])).days
    except ValueError:
        return 0.0
    if not 0 <= age < 30:
        return 0.0
    return 1.0 if age < 3 else 0.8 if age < 7 else 0.6


def ranking_score(item, today: date) -> float:
    # Never mutate base score: repeated selection must not compound decay.
    factor = policy_authority(item) if item.category == '政策' else freshness_factor(item, today)
    return round(item.score * factor, 2)


def policy_authority(item) -> float:
    """Use verified source URLs, never a model's guess from a media/title name.

    Central-government origins: 100%; local official origins/media: 80%;
    other/unverified media: 70%. National reprints qualify when their original
    central-government URL is present among the merged evidence sources.
    """
    national = {'www.gov.cn', 'gov.cn', 'nmpa.gov.cn', 'miit.gov.cn',
                'moe.gov.cn', 'most.gov.cn', 'nhc.gov.cn', 'samr.gov.cn',
                'cac.gov.cn', 'ndrc.gov.cn', 'npc.gov.cn', 'stats.gov.cn',
                'mof.gov.cn', 'mohrss.gov.cn', 'cnipa.gov.cn', 'sac.gov.cn'}
    local_media = {'bjd.com.cn', 'bjnews.com.cn', 'xinmin.cn', 'cnr.cn',
                   'people.com.cn', 'xinhuanet.com', 'news.cn', 'cctv.com',
                   'stdaily.com', 'southcn.com', 'sznews.com', 'sztv.com.cn'}
    hosts = {(urlsplit(s.get('url', '')).hostname or '').lower() for s in item.sources}
    hosts.add((urlsplit(item.url).hostname or '').lower())
    if any(h == d or (d not in {'gov.cn', 'www.gov.cn'} and h.endswith('.' + d))
           for h in hosts for d in national):
        return 1.0
    if any(h.endswith('.gov.cn') or any(h == d or h.endswith('.' + d) for d in local_media) for h in hosts):
        return .8
    return .7


@dataclass
class Article:
    title: str
    url: str
    published: str
    source: str
    summary: str = ""
    body: str = ""
    category: str = "行业"
    sources: list[dict] = field(default_factory=list)
    matches: list[dict] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    relevance: int | None = None
    score: float = 0
    exploration: bool = False
    accepted: bool = True
    origin: str = ""
    summary_kind: str = "source"
    display_title: str = ""
    source_summary: str = ""
    source_excerpt: str = ""
    summary_version: str = ""

    def __post_init__(self):
        self.url = canonical(self.url)
        if not self.sources and self.url:
            self.sources = [{"name": self.source, "url": self.url, "origin": self.origin}]
        self.capture_source()
        if not self.body:
            self.body = self.source_excerpt

    def capture_source(self):
        if self.summary_kind not in {'ai', 'failed'} and self.summary:
            self.source_summary = self.summary[:6000]
        if self.body:
            self.source_excerpt = self.body[:6000]

    @property
    def identity(self):
        return digest(self.url)

    def aliases(self):
        aliases = [digest("url:" + canonical(s["url"])) for s in self.sources] + [
            digest("title:" + title_key(self.title))]
        if self.category == '政策':
            # Stable standard number survives rewritten headlines and reprints.
            for prefix, number, year in re.findall(r'\b(YY\s*/?\s*T|GB\s*/?\s*T)\s*(\d{3,6})\s*[-—–－]\s*(20\d{2})',
                                                   self.title + ' ' + self.summary, re.I):
                aliases.append(digest('standard:' + re.sub(r'\s|/', '', prefix.upper()) + number + ':' + year))
        return aliases

    def in_window(self, today: date, windows: dict) -> bool:
        try:
            age = (today - date.fromisoformat(self.published[:10])).days
            return 0 <= age < windows[self.category]
        except (ValueError, KeyError):
            return False

    def record(self):
        self.capture_source()
        value = asdict(self)
        value.pop("body")  # Never persist full articles.
        return value
