from __future__ import annotations

import ipaddress
import json
import re
import socket
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from repo_courier.feeds import BEIJING, SearchWindow, parse_feed
from .model import Article, canonical, dictionary_result


def date_text(text):
    text = str(text)
    m = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", text)
    if m:
        try:
            return date(*map(int, m.groups())).isoformat()
        except ValueError:
            pass
    for pattern, formats in (
        (r"\b[A-Za-z]{3,9} \d{1,2}, \d{4}\b", ("%B %d, %Y", "%b %d, %Y")),
        (r"\b\d{1,2} [A-Za-z]{3,9} \d{4}\b", ("%d %B %Y", "%d %b %Y")),
    ):
        for match in re.finditer(pattern, text):
            for fmt in formats:
                try:
                    return datetime.strptime(match.group(), fmt).date().isoformat()
                except ValueError:
                    pass
    return ""


def public_url(url):
    p = urlsplit(url)
    if not canonical(url) or p.port not in (None, 80, 443):
        raise ValueError("非公开HTTP地址")
    host = p.hostname
    if host.endswith((".local", ".internal", ".localhost")) or host == "localhost":
        raise ValueError("拒绝本地地址")
    for record in socket.getaddrinfo(host, p.port or 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(record[4][0]).is_global:
            raise ValueError("拒绝非公网地址")


class Reader:
    def __init__(self):
        self.client = httpx.Client(timeout=20, follow_redirects=False, trust_env=False,
                                   headers={"User-Agent": "BrainsongToday/1.0"})
        self.robots = {}
        self.last_arxiv = 0.0
        self.last_host = {}
        self.observations = []

    def arxiv_api(self, url):
        # Documented programmatic API, not a crawl of arxiv's web pages.
        # https://info.arxiv.org/help/api/user-manual.html
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "export.arxiv.org" or parsed.path != "/api/query":
            raise ValueError("只允许官方arXiv查询接口")
        delay = 3 - (time.monotonic() - self.last_arxiv)
        if delay > 0:
            time.sleep(delay)
        self.last_arxiv = time.monotonic()
        return self.raw(url)

    def raw(self, url):
        for _ in range(4):
            public_url(url)
            p = urlsplit(url)
            root = f"{p.scheme}://{p.netloc}"
            policy = self.robots.get(root)
            interval = max(1, (policy.crawl_delay("BrainsongToday") or policy.crawl_delay("*") or 0) if policy else 0)
            if policy and not policy.can_fetch("BrainsongToday", url):
                raise ValueError("robots禁止抓取")
            delay = interval - (time.monotonic() - self.last_host.get(root, 0))
            if delay > 0:
                time.sleep(delay)
            self.last_host[root] = time.monotonic()
            with self.client.stream("GET", url) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers["location"])
                    continue
                response.raise_for_status()
                kind = response.headers.get("content-type", "")
                if any(t in kind for t in ("image/", "video/", "audio/", "application/pdf")):
                    raise ValueError("只读取公开文本")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 2_000_000:
                        raise ValueError("响应过大")
                    chunks.append(chunk)
                return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
        raise ValueError("重定向过多")

    def get(self, url):
        p = urlsplit(url)
        root = f"{p.scheme}://{p.netloc}"
        if root not in self.robots:
            parser = RobotFileParser(root + "/robots.txt")
            try:
                parser.parse(self.raw(root + "/robots.txt").splitlines())
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in (404, 410):
                    raise
                parser.parse([])
            self.robots[root] = parser
        if not self.robots[root].can_fetch("BrainsongToday", url):
            raise ValueError("robots禁止抓取")
        return self.raw(url)


def metadata(item, html):
    if urlsplit(item.url).hostname == "mp.weixin.qq.com":
        from .wechat import parse_article
        parsed = parse_article(item.url, html)
        item.title, item.published, item.body = parsed.title, parsed.published, parsed.body
        item.source, item.sources = parsed.source, parsed.sources
        if parsed.summary:
            item.summary = parsed.summary
        return item
    soup = BeautifulSoup(html, "html.parser")
    # The arXiv meta description labels the page; use the actual abstract.
    if (urlsplit(item.url).hostname or '') in {'arxiv.org', 'www.arxiv.org'}:
        abstract = soup.select_one('blockquote.abstract')
        if abstract:
            item.summary = re.sub(r'^\s*Abstract:\s*', '', abstract.get_text(' ', strip=True))
            item.summary_kind = 'source'
    for node in soup.select("meta[property],meta[name]"):
        key = node.get("property", node.get("name", "")).lower()
        value = node.get("content", "")
        if key in {"article:published_time", "date", "pubdate", "publishdate", "pubtime", "dc.date.issued"}:
            item.published = date_text(value) or item.published
        if not item.summary and key in {"description", "og:description"}:
            item.summary = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.get_text())
        except ValueError:
            continue
        stack = value if isinstance(value, list) else [value]
        for _ in range(60):
            if not stack:
                break
            node = stack.pop(0)
            if not isinstance(node, dict):
                continue
            if isinstance(node.get("@graph"), list):
                stack.extend(node["@graph"])
            types = node.get("@type", [])
            types = [types] if isinstance(types, str) else types
            if isinstance(types, list) and set(types) & {"Article", "NewsArticle", "BlogPosting", "ScholarlyArticle"}:
                item.published = date_text(node.get("datePublished", "")) or item.published
                item.summary = item.summary or str(node.get("description", ""))
    for node in soup.select("script,style,nav,footer,header,aside"):
        node.decompose()
    content = soup.select_one("article, .article-content, .TRS_Editor, main") or soup
    item.body = content.get_text("\n", strip=True)[:20000]
    if not item.published:
        selector = "time, .date, .time, .publish-time, .article-info, .releaseTime, .detail-info"
        if (urlsplit(item.url).hostname or "").endswith(".eastmoney.com"):
            selector += ", .time-source .item, .infos > .item"
        for node in soup.select(selector):
            item.published = date_text(node.get("datetime", "") + " " + node.get_text()) or item.published
    origin = re.search(r"(?:转载自|来源[：:])\s*([^\s，。<]{2,25})", item.body)
    if origin:
        item.origin = origin[1]
        item.sources[0]["origin"] = item.origin
    return item


def collect_wechat(root, reader, today):
    """Known links only. Does not use mptext, cookies, keys or paid services."""
    import yaml
    from .wechat import parse_article, validate_url
    config = yaml.safe_load((root / "config/wechat.yaml").read_text(encoding="utf-8")) or {}
    result = []
    for url in list(dict.fromkeys(config.get("article_urls", [])))[:20]:
        try:
            validate_url(url)
            result.append(parse_article(url, reader.get(url)))
        except Exception as exc:
            reader.observations.append({"source": "wechat", "stage": "known_link", "error": type(exc).__name__})
    return result


def from_search(rows, category="行业", domain=""):
    items = []
    for row in rows:
        url = canonical(str(row.get("link", "")))
        host = urlsplit(url).hostname or ""
        if not url or dictionary_result(url) or (domain and host != domain and not host.endswith("." + domain)):
            continue
        if not row.get("title"):
            continue
        items.append(Article(str(row["title"])[:250], url, date_text(row.get("publish_date", "")),
                             str(row.get("media") or host), str(row.get("content", ""))[:2000],
                             category=category, summary_kind="search"))
    return items


def bing_news(query, reader, today, category="行业", domain="", lookback_days=30):
    terms = (f"site:{domain} " if domain else "") + query
    spec = {"id": "bing-news", "name": "Bing新闻", "kind": "feed", "category": category,
            "url": "https://www.bing.com/news/search?" + urlencode({"q": terms, "format": "rss", "setlang": "zh-cn"})}
    rows = collect_source(spec, reader, today, [], lookback_days=lookback_days)
    output = []
    for item in rows:
        # Bing RSS can wrap the actual article URL. Only unwrap its explicit url parameter.
        if urlsplit(item.url).hostname in {"www.bing.com", "bing.com"}:
            from urllib.parse import parse_qs
            target = parse_qs(urlsplit(item.url).query).get("url", [""])[0]
            item.url = canonical(target)
        host = urlsplit(item.url).hostname or ""
        if not item.url or host in {"www.bing.com", "bing.com"}:
            continue
        if domain and host != domain and not host.endswith("." + domain):
            continue
        item.source = host
        item.sources = [{"name": host, "url": item.url, "origin": ""}]
        item.summary_kind = "search"
        output.append(item)
    return output


def parse_listing(raw, spec):
    """Port desktop card parsing; never borrow a neighbouring article's date."""
    soup = BeautifulSoup(raw, "html.parser")
    if any(s in soup.get_text(" ", strip=True)[:1500] for s in
           ("正在进行安全检测", "Just a moment...", "您访问的网址有误")):
        raise ValueError("来源需要验证或页面不可用")
    for node in soup.select("script,style,nav,footer,header"):
        node.decompose()
    selector = spec.get("links", "article a[href]")
    containers = {id(n) for n in soup.select(spec["container"])} if spec.get("container") else set()
    result = {}
    for link in soup.select(selector):
        href = canonical(urljoin(spec["url"], link.get("href", "")))
        heading = link.select_one("h1,h2,h3,h4,h5,h6,[class*='_h3'],[class*='post-title']")
        title = heading.get_text(" ", strip=True) if heading else link.get("title") or link.get_text(" ", strip=True)
        if not href or href == canonical(spec["url"]) or not 8 <= len(title) <= 300:
            continue
        card = link
        for parent in list(link.parents)[:5]:
            others = {canonical(urljoin(spec["url"], a.get("href", ""))) for a in parent.select(selector)}
            if others - {href}:
                break
            card = parent
            if id(parent) in containers or date_text(parent.get_text(" ", strip=True)):
                break
        published = date_text(card.get_text(" ", strip=True))
        for node in card.select("time"):
            published = date_text(node.get("datetime", "")) or published
        paragraphs = [p.get_text(" ", strip=True) for p in card.select("p")]
        summary = max(paragraphs, key=len, default="")[:2000]
        item = Article(title, href, published, spec["name"], summary, category=spec.get("category", "行业"))
        previous = result.get(href)
        if previous:
            previous.published = previous.published or published
            previous.summary = max((previous.summary, summary), key=len)
        else:
            result[href] = item
    if not result:
        raise ValueError("页面已读取，但未解析出文章条目")
    return list(result.values())[:100]


def collect_source(spec, reader, today, rules, *, filter_relevance=True, lookback_days=30):
    kind = spec["kind"]
    url = spec["url"]
    category = spec.get("category", "行业")
    if kind == "arxiv":
        query = spec.get("query", '(cat:q-bio.NC OR cat:cs.HC OR cat:eess.SP OR cat:cs.LG) AND (all:"EEG" OR all:"ear-EEG" OR all:"brain-computer interface")')
        url += "?" + urlencode({"search_query": query, "sortBy": "submittedDate", "sortOrder": "descending", "max_results": 80})
    try:
        raw = reader.arxiv_api(url) if kind == "arxiv" else reader.get(url)
    except Exception as exc:
        if kind != "arxiv":
            raise
        reader.observations.append({"source": spec["id"], "stage": "arxiv_api", "error": str(exc), "fallback": True})
        raw = reader.get("https://rss.arxiv.org/rss/q-bio.NC+cs.HC+eess.SP")
    if kind in {"feed", "arxiv"}:
        # A three-day window means today plus the two preceding calendar days.
        start = today-timedelta(days=max(0, lookback_days-1))
        window = SearchWindow(datetime.combine(start, datetime.min.time(), BEIJING),
                              datetime.combine(today, datetime.max.time(), BEIJING))
        entries = parse_feed(raw, window, limit=100, content_token_limit=6000)
        return [Article(e.title, re.sub(r"v\d+$", "", e.url.replace("http://arxiv.org/", "https://arxiv.org/")) if kind == "arxiv" else e.url, e.published_at.astimezone(BEIJING).date().isoformat(),
                        spec["name"], e.summary, e.content, category=category) for e in entries]
    if kind == "gov_json":
        return [Article(r["TITLE"], r["URL"], date_text(r.get("DOCRELPUBTIME", "")), spec["name"],
                        r.get("SUB_TITLE", ""), category="政策") for r in json.loads(raw)[:100]]
    if kind != "html":
        return []
    result = parse_listing(raw, spec)
    if not filter_relevance:
        return result
    from repo_courier.matching import match_rules
    candidates = [a for a in result if match_rules(a.title, a.summary, "", rules)]
    # Trusted specialist/company channels can supply evidence missing in a short headline.
    candidates += [a for a in result if a not in candidates and
                   (spec.get("company") or spec.get("specialist") or len(a.summary.strip()) < 30)][:6]
    candidates.sort(key=lambda a: max((r["weight"] for r in match_rules(a.title, a.summary, a.body, rules)), default=0), reverse=True)
    for item in candidates[:6]:
        if not item.published or len(item.summary.strip()) < 30:
            try:
                metadata(item, reader.get(item.url))
            except Exception as exc:
                reader.observations.append({"source": spec["id"], "url": item.url, "stage": "metadata", "error": str(exc)})
    candidates.sort(key=lambda a: max((r["weight"] for r in match_rules(a.title, a.summary, a.body, rules)), default=0), reverse=True)
    return candidates[:40]
