"""Conservative, cached publisher reputation based on verifiable URL provenance.

The first encounter creates one row per publisher.  A search snippet's claim
that a page is an "original" is deliberately not evidence of publisher ownership.
"""
from urllib.parse import urlsplit

from .model import source_key


def rating(item, scoring):
    host = (urlsplit(item.url).hostname or '').lower()
    for domain, values in sorted(scoring.get('source_domains', {}).items(),
                                 key=lambda pair: len(pair[0]), reverse=True):
        if host == domain or host.endswith('.' + domain):
            return values[0], values[1], '已配置的独立域名'
    if host == 'gov.cn' or host.endswith('.gov.cn'):
        return 100, 85, '政府机构域名'
    if host.endswith('.edu.cn') or host.endswith('.ac.cn'):
        return 80, 45, '高校或科研机构域名'
    if host == 'mp.weixin.qq.com':
        return 40, 40, '公众号身份未独立核实'
    if host.endswith('.sohu.com') or host == 'sohu.com' or host.endswith('.163.com'):
        return 40, 60, '媒体平台账号未独立核实'
    if host == 'china.com' or host.endswith('.china.com'):
        return 45, 60, '转载或分发平台'
    if host:
        return 50, 40, '新域名待核实'
    return 0, 0, '无有效发布者'


def attach(item, state, scoring, today):
    key = source_key(item)
    record = state.source_authority(key)
    authority, recognition, basis = rating(item, scoring)
    if record is None:
        state.record_source_authority(key, item.source, item.url, authority,
                                      recognition, basis, today)
    elif basis == '已配置的独立域名' and (
            record['basis'] != basis or record['authority'] != authority
            or record['recognition'] != recognition):
        # An explicit editorial override may promote a previously unknown site.
        state.update_source_authority(key, authority, recognition, basis)
    else:
        authority, recognition = record['authority'], record['recognition']
    item.source_authority = authority
    item.source_recognition = recognition
    return item
