"""Static-text adapter informed by oldjie/wechat-article-parser (MIT).

Uses the same article selectors, without executing remote scripts, loading images,
installing Chromium, collecting login cookies or bypassing verification pages.
Not an account-history crawler. Browser-only pages are reported as unavailable.
"""
import re
from datetime import datetime
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from repo_courier.feeds import BEIJING
from .model import Article
from .provider import QualityError


def validate_url(url):
    p = urlsplit(url)
    if p.scheme != "https" or p.hostname != "mp.weixin.qq.com" or p.username or p.password or p.port not in (None, 443) or not (p.path == "/s" or p.path.startswith("/s/")):
        raise QualityError("wechat_invalid_url")


def parse_article(url, html):
    from .collect import date_text
    validate_url(url)
    soup = BeautifulSoup(html, "html.parser")
    content = soup.select_one("#js_content")
    title = soup.select_one("#activity-name")
    if not content or not title:
        raise QualityError("wechat_unavailable_or_verification")
    for node in content.select("script,style,noscript,img,video,audio"):
        node.decompose()
    body = content.get_text("\n", strip=True)[:20000]
    if len(body) < 30:
        raise QualityError("wechat_empty_body")
    author = soup.select_one("#js_name")
    published_node = soup.select_one("#publish_time")
    published = date_text(published_node.get_text()) if published_node else ""
    if not published:
        timestamp = re.search(r"\b(?:var\s+)?(?:ct|create_time)\s*[:=]\s*['\"]?(\d{10})\b", html)
        if timestamp:
            published = datetime.fromtimestamp(int(timestamp[1]), BEIJING).date().isoformat()
    desc = soup.select_one('meta[property="og:description"]')
    return Article(title.get_text(" ", strip=True), url, published,
                   author.get_text(" ", strip=True) if author else "微信公众号",
                   desc.get("content", "") if desc else "", body)
