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


def freshness_factor(item, today: date, scoring=None) -> float:
    """Calendar-day tiers; date-only sources cannot support exact 24h ages."""
    try:
        age = (today - date.fromisoformat((item.first_reported or item.published)[:10])).days
    except ValueError:
        return 0.0
    if not 0 <= age < 30:
        return 0.0
    for upper, factor in (scoring or {}).get('freshness', [[1, 1], [3, .8], [7, .45], [14, .2], [30, .05]]):
        if age < upper:
            return factor
    return 0.0


def source_factor(item, scoring=None):
    scoring = scoring or {}
    host = (urlsplit(item.url).hostname or '').lower()
    ratings = scoring.get('source_reputation', {'original':[85,60], 'media':[80,70], 'secondary':[60,50], 'unknown':[0,0]})
    authority, recognition = ratings.get(item.source_kind, [0,0])
    if host.endswith('.gov.cn') or host == 'gov.cn':
        authority, recognition = 100, 85
    if host in {'arxiv.org', 'www.arxiv.org'}:
        authority, recognition = 70, 85  # Preprints are not peer-review certification.
    domains = scoring.get('source_domains', {})
    for domain in sorted(domains, key=len, reverse=True):
        if host == domain or host.endswith('.' + domain):
            authority, recognition = domains[domain]
            break
    weights = scoring.get('source_weights', {'authority': .7, 'recognition': .3})
    return (authority * weights['authority'] + recognition * weights['recognition']) / 100


def shortlist_score(item, today: date, scoring=None) -> float:
    base = item.relevance if item.relevance is not None else item.score
    return round(base * freshness_factor(item, today, scoring), 4)


def ranking_score(item, today: date, scoring=None) -> float:
    # Never mutate base score: repeated selection must not compound decay.
    base = item.relevance if item.relevance is not None else item.score
    return round(base * source_factor(item, scoring), 4)


def star_text(score):
    import math
    halves = max(0, min(10, math.floor(score / 10 + .5)))
    return '★' * (halves // 2) + ('☆' if halves % 2 else '')


def company_keys(item, scoring=None):
    aliases = (scoring or {}).get('company_aliases', {})
    text = item.title + ' ' + item.source_summary
    found = set()
    for company, names in aliases.items():
        if any(re.search(r'(?<![A-Za-z])' + re.escape(n) + r'(?![A-Za-z])', text, re.I) for n in names):
            found.add(company.casefold())
    for name in item.companies:
        canonical_name = next((key for key, names in aliases.items() if name.casefold() in [n.casefold() for n in names]), name)
        found.add(canonical_name.casefold())
    return found


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
    event_type: str = 'ordinary'
    source_kind: str = 'secondary'
    companies: list[str] = field(default_factory=list)
    first_reported: str = ''
    subject: str = ''
    recommendation_score: float | None = None

    def __post_init__(self):
        self.url = canonical(self.url)
        if not self.sources and self.url:
            self.sources = [{"name": self.source, "url": self.url, "origin": self.origin}]
        self.capture_source()
        if not self.body:
            self.body = self.source_excerpt

    def capture_source(self):
        if self.summary_kind not in {'ai', 'failed', 'formatted'} and self.summary:
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
