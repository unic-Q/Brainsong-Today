from __future__ import annotations

import ipaddress
import json
import re
import socket
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
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


def url_date(url):
    """Dates encoded in known article URL formats, never arbitrary numeric IDs."""
    parsed = urlsplit(url)
    host, path = (parsed.hostname or '').lower(), parsed.path
    if host.endswith('.eastmoney.com'):
        match = re.fullmatch(r'/a/(20\d{6})\d+\.html', path)
    elif host in {'chinaventure.com.cn', 'www.chinaventure.com.cn'}:
        match = re.fullmatch(r'/news/\d+-(20\d{6})-\d+\.html', path)
    else:
        match = None
    if not match:
        return ''
    try:
        return datetime.strptime(match[1], '%Y%m%d').date().isoformat()
    except ValueError:
        return ''


def date_hint(text):
    """Diagnostic clue only; never use this to establish a publication date."""
    return bool(re.search(r'20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}|(?<!\d)\d{1,2}[-/月]\d{1,2}(?:日|\b)', str(text)))


def bciwiki_listing_date(card):
    """Use the card's month/day and its year group, not dates in the summary."""
    group = card.find_previous(class_='bw-group')
    meta = card.select_one('.bw-card__meta span')
    if not group or not meta:
        return ''
    year_month = re.search(r'(20\d{2})\s*年\s*(\d{1,2})\s*月', group.get_text(' ', strip=True))
    month_day = re.fullmatch(r'\s*(\d{1,2})[-/]\s*(\d{1,2})\s*', meta.get_text(' ', strip=True))
    if not year_month or not month_day or int(year_month[2]) != int(month_day[1]):
        return ''
    try:
        return date(int(year_month[1]), int(month_day[1]), int(month_day[2])).isoformat()
    except ValueError:
        return ''


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
        if item.published:
            item.date_evidence = 'article_visible'
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
            found = date_text(value)
            if found:
                item.published, item.date_evidence = found, 'article_meta'
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
                found = date_text(node.get("datePublished", ""))
                if found:
                    item.published, item.date_evidence = found, 'article_meta'
                item.summary = item.summary or str(node.get("description", ""))
    # Publication time often sits in the page header or sidebar. Read it before
    # removing those elements from the article body.
    host = (urlsplit(item.url).hostname or '').lower()
    selectors = {
        'www.stcn.com': '.detail-info',
        'www.chinaventure.com.cn': '.releaseTime',
        'chinaventure.com.cn': '.releaseTime',
    }
    selector = selectors.get(host, 'time, .date, .time, .publish-time, .article-info, .releaseTime, .detail-info')
    if host.endswith('.eastmoney.com'):
        selector = '.infos, .time-source .item, time, .date, .publish-time'
    if item.date_evidence != 'article_meta':
        for node in soup.select(selector):
            found = date_text(node.get('datetime', '') + ' ' + node.get_text(' ', strip=True))
            if found:
                item.published, item.date_evidence = found, 'article_visible'
                break
    if not item.published:
        found = url_date(item.url)
        if found:
            item.published, item.date_evidence = found, 'url'
    if not item.published:
        item.date_evidence = 'hint_unparsed' if date_hint(soup.get_text(' ', strip=True)[:800]) else 'no_date_hint'
    for node in soup.select("script,style,nav,footer,header,aside"):
        node.decompose()
    content = soup.select_one("article, .article-content, .TRS_Editor, main") or soup
    item.body = content.get_text("\n", strip=True)[:20000]
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
        published = date_text(row.get('publish_date', ''))
        evidence = 'search' if published else ''
        if not published:
            published, evidence = url_date(url), 'url'
        if not published:
            evidence = 'hint_unparsed' if date_hint(row.get('publish_date', '')) else 'no_date_hint'
        items.append(Article(str(row["title"])[:250], url, published,
                             str(row.get("media") or host), str(row.get("content", ""))[:2000],
                             category=category, summary_kind="search", date_evidence=evidence))
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
        if spec['id'] in {'eastmoney', 'chinaventure'} and not url_date(href):
            continue
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
        if spec['id'] in {'bciwiki-policy', 'bciwiki-funding'}:
            card = link.find_parent('article', class_='bw-card') or card
            published = bciwiki_listing_date(card)
        elif spec['id'] == 'stcn':
            # The card excerpt quotes events from other days; its dates are
            # not the article's publication time.
            published = ''
            for node in card.select('time, .date, .publish-time'):
                published = date_text(node.get('datetime', '') + ' ' + node.get_text(' ', strip=True)) or published
        else:
            published = date_text(card.get_text(" ", strip=True))
            for node in card.select("time"):
                published = date_text(node.get("datetime", "")) or published
        evidence = 'listing' if published else ''
        trusted_url_date = url_date(href)
        if trusted_url_date:
            published, evidence = trusted_url_date, 'url'
        if not published:
            evidence = 'hint_unparsed' if date_hint(card.get_text(' ', strip=True)) else 'no_date_hint'
        paragraphs = [p.get_text(" ", strip=True) for p in card.select("p")]
        summary = max(paragraphs, key=len, default="")[:2000]
        if spec['id'] in {'bciwiki-policy', 'bciwiki-funding'}:
            summary_node = card.select_one('.bw-card__summary')
            if summary_node:
                summary = summary_node.get_text(' ', strip=True)[:2000]
        item = Article(title, href, published, spec["name"], summary,
                       category=spec.get("category", "行业"), date_evidence=evidence)
        previous = result.get(href)
        if previous:
            previous.published = previous.published or published
            if not previous.date_evidence or previous.date_evidence in {'no_date_hint', 'hint_unparsed'}:
                previous.date_evidence = evidence
            previous.summary = max((previous.summary, summary), key=len)
        else:
            result[href] = item
    if not result:
        raise ValueError("页面已读取，但未解析出文章条目")
    return list(result.values())[:100]


def record_date_parse(reader, source_id, items):
    if hasattr(reader, 'observations'):
        reader.observations.append({'source': source_id, 'stage': 'date_parse',
                                    'parsed': len(items), 'dated': sum(bool(a.published) for a in items),
                                    'hint_unparsed': sum(a.date_evidence == 'hint_unparsed' for a in items),
                                    'no_date_hint': sum(a.date_evidence == 'no_date_hint' for a in items)})


def arxiv_entries(raw, today, lookback_days, source):
    """Accept only arXiv Atom entries with explicit submission/version dates."""
    root = ET.fromstring(raw)
    atom = '{http://www.w3.org/2005/Atom}'
    start = today - timedelta(days=max(0, lookback_days - 1))
    result = []
    for entry in root.findall(atom + 'entry'):
        first_raw = entry.findtext(atom + 'published', default='')
        revised_raw = entry.findtext(atom + 'updated', default='')
        try:
            first = datetime.fromisoformat(first_raw.replace('Z', '+00:00'))
            revised = datetime.fromisoformat(revised_raw.replace('Z', '+00:00'))
            if first.tzinfo is None or revised.tzinfo is None or revised < first:
                continue
            first_day = first.astimezone(timezone.utc).date()
            revised_day = revised.astimezone(timezone.utc).date()
        except ValueError:
            continue
        if not start <= revised_day <= today:
            continue
        raw_id = entry.findtext(atom + 'id', default='').strip()
        match = re.fullmatch(r'https?://(?:www\.)?arxiv\.org/abs/([^/?#]+?)(v\d+)?', raw_id)
        if not match:
            continue
        url = 'https://arxiv.org/abs/' + match[1]
        title = re.sub(r'\s+', ' ', entry.findtext(atom + 'title', default='')).strip()
        summary = re.sub(r'\s+', ' ', entry.findtext(atom + 'summary', default='')).strip()
        if not title or not summary:
            continue
        result.append(Article(title, url, revised_day.isoformat(), source, summary,
                              category='学术', date_evidence='arxiv_version',
                              first_submitted=first_day.isoformat(),
                              last_revised=revised_day.isoformat(),
                              arxiv_version=match[2] or ('v1' if first == revised else '')))
    return result


def collect_source(spec, reader, today, rules, *, filter_relevance=True, lookback_days=30):
    kind = spec["kind"]
    url = spec["url"]
    category = spec.get("category", "行业")
    if kind == "arxiv":
        query = spec.get("query", '(cat:q-bio.NC OR cat:cs.HC OR cat:eess.SP OR cat:cs.LG) AND (all:"EEG" OR all:"ear-EEG" OR all:"brain-computer interface")')
        url += "?" + urlencode({"search_query": query, "sortBy": "lastUpdatedDate", "sortOrder": "descending", "max_results": 80})
    try:
        raw = reader.arxiv_api(url) if kind == "arxiv" else reader.get(url)
    except Exception as exc:
        if kind != "arxiv":
            raise
        reader.observations.append({"source": spec["id"], "stage": "arxiv_api", "error": str(exc), "fallback": True})
        raw = reader.get("https://rss.arxiv.org/rss/q-bio.NC+cs.HC+eess.SP")
    if kind == 'arxiv':
        result = arxiv_entries(raw, today, lookback_days, spec['name'])
        record_date_parse(reader, spec['id'], result)
        return result
    if kind == "feed":
        # A three-day window means today plus the two preceding calendar days.
        start = today-timedelta(days=max(0, lookback_days-1))
        window = SearchWindow(datetime.combine(start, datetime.min.time(), BEIJING),
                              datetime.combine(today, datetime.max.time(), BEIJING))
        entries = parse_feed(raw, window, limit=100, content_token_limit=6000)
        result = [Article(e.title, e.url, e.published_at.astimezone(BEIJING).date().isoformat(),
                          spec["name"], e.summary, e.content, category=category, date_evidence='feed') for e in entries]
        record_date_parse(reader, spec['id'], result)
        return result
    if kind == "gov_json":
        result = []
        for row in json.loads(raw)[:100]:
            published = date_text(row.get('DOCRELPUBTIME', ''))
            evidence = 'api' if published else ('hint_unparsed' if date_hint(row.get('DOCRELPUBTIME', '')) else 'no_date_hint')
            result.append(Article(row['TITLE'], row['URL'], published, spec['name'],
                                  row.get('SUB_TITLE', ''), category='政策', date_evidence=evidence))
        record_date_parse(reader, spec['id'], result)
        return result
    if kind != "html":
        return []
    result = parse_listing(raw, spec)
    record_date_parse(reader, spec['id'], result)
    if not filter_relevance:
        if spec['id'] == 'stcn':
            # Numeric STCN URLs carry no date; only open directly relevant
            # article pages, never the whole broad finance listing.
            relevant = [a for a in result if not a.published and re.search(
                r'脑机|脑电|耳周|神经接口|\bEEG\b|\bBCI\b', a.title + ' ' + a.summary[:500], re.I)]
            for item in relevant[:6]:
                try:
                    metadata(item, reader.get(item.url))
                except Exception as exc:
                    if hasattr(reader, 'observations'):
                        reader.observations.append({'source': spec['id'], 'stage': 'article_date',
                                                    'url': item.url, 'error': type(exc).__name__})
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
