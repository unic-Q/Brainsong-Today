"""Conservative, bounded discovery of a company's actual product page."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .model import canonical, digest

NON_OFFICIAL_HOSTS = {
    'ithome.com', '36kr.com', 'stcn.com', 'pedaily.cn', 'bciwiki.com',
    'theverge.com', 'techcrunch.com', 'amazon.com', 'taobao.com',
    'tmall.com', 'jd.com', 'youtube.com', 'weibo.com', 'zhihu.com',
}
NON_PRODUCT_PATH = re.compile(r'/(?:news|newsroom|blog|press|article|review|'
                              r'support|faq|about|career|download)(?:/|$)', re.I)
RELEASE_WORDS = re.compile(r'发布|推出|上线|上市|开售|发售|首发|亮相|'
                           r'launch(?:ed|es)?|unveil(?:ed|s)?|introduc(?:ed|es)', re.I)
GENERIC_PRODUCTS = {'耳机', '脑电耳机', '眼镜', 'ai眼镜', '产品', '新品', '设备', '系统'}


def _root_domain(host):
    parts = host.lower().split('.')
    if len(parts) >= 3 and '.'.join(parts[-2:]) in {'com.cn', 'co.uk', 'com.hk'}:
        return '.'.join(parts[-3:])
    return '.'.join(parts[-2:]) if len(parts) >= 2 else host


def _text_key(value):
    return re.sub(r'[^\w\u4e00-\u9fff]', '', value.casefold())


def _company(item):
    return item.companies[0] if item.companies else item.subject


def _official_domains(item, sources):
    company = _company(item).casefold()
    domains = set()
    for source in sources:
        name = str(source.get('company', '')).casefold()
        if name and company and (name in company or company in name):
            host = urlsplit(source.get('url', '')).hostname or ''
            if host:
                domains.add(_root_domain(host))
    if item.source_kind == 'original':
        host = urlsplit(item.url).hostname or ''
        root = _root_domain(host)
        if root and not any(root == bad or root.endswith('.' + bad)
                            for bad in NON_OFFICIAL_HOSTS):
            domains.add(root)
    return domains


def _looks_official(url, company, domains, row=None):
    host = (urlsplit(url).hostname or '').lower()
    root = _root_domain(host)
    if any(root == bad or root.endswith('.' + bad) for bad in NON_OFFICIAL_HOSTS):
        return False
    if domains:
        return root in domains
    # Without a known official domain, only a distinct Latin brand in the
    # hostname is sufficient to attempt page verification. Never guess pinyin.
    words = re.findall(r'[a-z][a-z0-9]{3,}', company.casefold())
    if any(word in root for word in words if word not in {'technology', 'company', 'limited'}):
        return True
    media = _text_key(str((row or {}).get('media', '')))
    brand = _text_key(company)
    return len(brand) >= 3 and brand in media and ('官网' in media or 'official' in media)


def _page_matches(html, product_name, company='', require_company=False):
    soup = BeautifulSoup(html, 'html.parser')
    headings = ' '.join(node.get_text(' ', strip=True)
                        for node in soup.select('title, h1, h2, [itemprop="name"]')[:15])
    headings += ' ' + ' '.join(node.get('content', '') for node in soup.select(
        'meta[property="og:title"], meta[name="description"]')[:3])
    product = _text_key(product_name)
    if not product or product not in _text_key(headings):
        return False
    return not require_company or _text_key(company) in _text_key(
        soup.get_text(' ', strip=True)[:6000])


def detect_product_launches(items, provider, state, today, *, offline=False, no_ai=False):
    """Read source evidence only for selected items whose final summary says 'released'."""
    for item in items:
        item.event_type, item.product_name, item.product_url = 'ordinary', '', ''
    if offline or no_ai:
        return
    pending = []
    for item in items:
        if not RELEASE_WORDS.search(item.summary or ''):
            continue
        evidence = ' '.join((item.title, item.summary,
                             (item.body or item.source_excerpt)[:3500],
                             item.source_summary[:1000]))
        signature = digest(evidence)
        key = 'launch-v1:' + item.identity + ':' + signature
        cached = state.get(key)
        if cached is not None:
            _apply_launch(item, cached, evidence)
        else:
            pending.append((item, key, evidence))
    if not pending:
        return
    payload = [{'id': f'P{i}', 'title': item.title, 'summary': item.summary,
                'source_evidence': evidence[:4500]}
               for i, (item, _, evidence) in enumerate(pending)]
    try:
        result = provider.chat(
            '只判断这些已入选、摘要包含发布类字眼的资讯到底发布了什么。'
            '论文、政策、报告、融资公告、旧品评测不属于产品新品发布。'
            '若确为具体新品发布，从原始材料提取公司和具体产品或型号名称；'
            '产品名可能只在正文，不要求出现在标题或摘要。不得猜测名称。'
            '返回JSON：{"items":[{"id":"原样ID","is_product_launch":true或false,'
            '"company":"企业原名或空","product_name":"具体产品/型号名或空"}]}。'
            '所有输入ID各返回一次，不要解释。', payload)
        rows = result.get('items', [])
        if not isinstance(rows, list):
            raise ValueError('产品发布判断格式错误')
        by_id = {row.get('id'): row for row in rows if isinstance(row, dict)}
        for i, (item, key, evidence) in enumerate(pending):
            row = by_id.get(f'P{i}')
            if not isinstance(row, dict):
                continue
            _apply_launch(item, row, evidence)
            state.put(key, {'is_product_launch': item.event_type == 'product',
                            'company': _company(item), 'product_name': item.product_name,
                            'day': today.isoformat()})
    except Exception as exc:
        state.error(today, '新品发布判断', exc)


def _apply_launch(item, row, evidence):
    if row.get('is_product_launch') is not True:
        return
    name = row.get('product_name', '')
    company = row.get('company', '')
    if not isinstance(name, str) or not isinstance(company, str):
        return
    name, company = name.strip(), company.strip()
    if (not 2 <= len(name) <= 80 or _text_key(name) in GENERIC_PRODUCTS
            or _text_key(name) not in _text_key(evidence)):
        return
    known_company = _company(item).strip()
    if (known_company and company and _text_key(company) in _text_key(evidence)
            and _text_key(company) not in _text_key(known_company)
            and _text_key(known_company) not in _text_key(company)):
        return
    if not known_company and company and _text_key(company) in _text_key(evidence):
        item.companies = [company]
        known_company = company
    if not known_company:
        return
    item.event_type, item.product_name = 'product', name


def enrich_product_pages(items, sources, provider, reader, state, today, *, offline=False):
    """Search once per selected product; failures never block the briefing."""
    if offline:
        return
    for item in items:
        if item.event_type != 'product' or not item.product_name:
            continue
        company = _company(item).strip()
        if not company:
            continue
        domains = _official_domains(item, sources)
        key = 'product-page-v1:' + digest(company.casefold() + '\0' + item.product_name.casefold())
        cached = state.get(key)
        if cached and (cached.get('url') or cached.get('day') == today.isoformat()):
            url = canonical(cached.get('url', ''))
            if url and (cached.get('verified') or _looks_official(url, company, domains)):
                item.product_url = url
            continue
        try:
            rows = provider.search(f'{company} {item.product_name} 官方 产品页', recency='')
            checked = 0
            for row in rows:
                url = canonical(str(row.get('link', '')))
                if (not url or url == item.url or NON_PRODUCT_PATH.search(urlsplit(url).path)
                        or not _looks_official(url, company, domains, row)):
                    continue
                checked += 1
                if checked > 3:
                    break
                try:
                    host_verified = _looks_official(url, company, domains)
                    if _page_matches(reader.get(url), item.product_name, company,
                                     require_company=not host_verified):
                        item.product_url = url
                        break
                except Exception as exc:
                    state.error(today, '产品页读取:' + url, exc)
            state.put(key, {'url': item.product_url, 'verified': bool(item.product_url),
                            'day': today.isoformat()})
        except Exception as exc:
            state.error(today, '产品页搜索:' + item.url, exc)
