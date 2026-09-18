"""One-time, explicit seed import. No database/settings reads, no plaintext secrets."""
import argparse
import json
import re
from pathlib import Path

import yaml


def groups(text):
    # Explicit overrides for the historical user's table; this is NOT a runtime
    # expression parser. Runtime only understands lists of OR-groups joined by AND.
    overrides = {
        '耳机 教育 应用': [['耳机', 'headphones', 'headset'], ['教育', '教学', '课堂', '学习', 'education', 'classroom']],
        '脑电耳机 (发布 OR 众筹 OR 评测)': [['脑电耳机', 'EEG耳机'], ['发布', '众筹', '评测']],
        '脑机接口 (融资 OR 投资 OR 并购)': [['脑机接口', 'BCI'], ['融资', '投资', '并购', 'funding']],
        '教育科技 (融资 OR 并购)': [['教育科技', 'edtech'], ['融资', '并购', 'funding'], ['脑电', '耳机', '可穿戴', 'EEG']],
        '(干电极 OR 耳夹电极) 供应商': [['干电极', '耳夹电极'], ['供应商', '供应', '制造']],
        '(STEAM教育 OR 特教) 脑电': [['STEAM教育', '特教'], ['脑电', 'EEG']],
        '(NMPA OR 医疗器械注册) 脑电': [['NMPA', '医疗器械注册'], ['脑电', 'EEG', '脑机']],
        '(FDA OR CE) 神经数据': [['FDA', 'CE'], ['神经数据', 'neural data']],
        '神经数据 (隐私 OR 伦理 OR 未成年人)': [['神经数据', '脑电数据', '脑机接口'], ['隐私', '伦理', '未成年人']],
        '脑机接口': [['脑机接口', 'brain-computer interface', 'brain computer interface', 'BCI']],
        'Muse': [['Muse'], ['EEG', '脑电', '冥想', 'headband', 'sleep']],
        '教育硬件渠道 OR 学校采购': [['教育硬件渠道', '学校采购'], ['脑电', '耳机', '可穿戴', 'EEG']],
        '课堂专注度 OR 学生注意力监测': [['课堂专注度', '学生注意力监测'], ['脑电', '耳机', '可穿戴', 'EEG']],
        '学习状态监测': [['学习状态监测'], ['脑电', '耳机', '可穿戴', 'EEG']],
        '注意力监测': [['注意力监测'], ['脑电', '耳机', '可穿戴', 'EEG']],
        '学生情绪监测': [['学生情绪监测'], ['脑电', '耳机', '可穿戴', 'EEG']],
        'auditory attention decoding': [['auditory attention decoding']],
        'EEG emotion recognition wearable': [['EEG'], ['emotion recognition'], ['wearable']],
        'ear-EEG education attention': [['ear-EEG', '耳周脑电'], ['education', 'attention']],
    }
    if text in overrides:
        return overrides[text]
    if ' OR ' in text:
        return [[x.strip().strip('"') for x in text.split(' OR ')]]
    if ' ' in text and not text.startswith('"'):
        return [[x] for x in text.split()]
    return [[text.strip('"')]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('desktop_assets')
    parser.add_argument('output')
    args = parser.parse_args()
    assets, output = Path(args.desktop_assets), Path(args.output)
    terms = json.loads((assets/'weighted_terms.json').read_text(encoding='utf-8'))
    rules, excluded = [], []
    for index, term in enumerate(terms):
        text = term['词']
        if term['权重'] < 0:
            # Exclude headline/snippet noise, not a whole relevant page with an ad footer.
            excluded.extend(x.strip() for x in text.split(' OR ') if x.strip() != '广告')
            continue
        topics = ['核心']
        purpose = '消费级脑电产品研发与商业决策相关技术、场景或产业信息'
        if any(x in text for x in ['教育', '课堂', '学习', 'ear-EEG education']):
            topics = ['耳机教育']
            purpose = '筛选耳机/脑电与教育应用的交叉信息，不看泛教育资讯'
        if any(x in text for x in ['科技', 'Muse', 'Emotiv', 'Neurable', 'NeuroSky', '脑韵', 'NextSense']):
            topics = ['企业']
            purpose = '了解消费级神经技术竞品及新企业的产品、商业进展'
        if any(x in text for x in ['融资', '投资', '并购']):
            topics = ['资本']
            purpose = '识别消费级脑机/相关教育硬件的新公司及融资并购'
        if any(x in text for x in ['NMPA', 'FDA', '隐私']):
            topics = ['政策']
            purpose = '识别与脑电硬件、神经数据及未成年人相关的监管要求'
        if any(x in text for x in ['数据集', 'foundation model', 'decoding']):
            topics.append('学术')
        rules.append({'id': f'r{index+1:02d}-{text.split(" OR ")[0].strip(chr(34))[:22]}',
                      'weight': term['权重'], 'groups': groups(text), 'purpose': purpose,
                      'topics': topics, 'query': re.sub(r'[()]', '', text).replace('"', '')[:55],
                      'exploration': term['探索'],
                      'direct': any(x in text for x in ['脑机', '脑电', 'EEG', 'Neurable', 'Emotiv', '脑韵'])})
    (output/'keywords.yaml').write_text(yaml.safe_dump({'exclude': excluded, 'rules': rules}, allow_unicode=True, sort_keys=False), encoding='utf-8')
    sources = json.loads((assets/'sources.json').read_text(encoding='utf-8'))
    categories = {'政策监管': '政策', '学术前沿': '学术'}
    sources = [dict(s, category=categories.get(s.get('category'), '学术' if s['kind']=='arxiv' else '行业')) for s in sources if s['kind'] != 'external']
    sources.append({'id': 'nmpa', 'name': '国家药品监督管理局', 'url': 'https://www.nmpa.gov.cn/',
                    'kind': 'search', 'category': '政策', 'enabled': True,
                    'query': '脑机接口 脑电 神经数据 医疗器械 指导原则'})
    # Failed public readers can be monitored with official search, without bypassing a gate.
    for s in sources:
        if not s.get('enabled'):
            s.update(kind='search', enabled=True, query=s['name']+' 脑电 耳机 新品')
    (output/'sources.yaml').write_text(yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding='utf-8')
    (output/'events.json').write_bytes((assets/'events.json').read_bytes())
    print(f'Imported {len(rules)} explicit rules, {len(sources)} source definitions; events remain unverified.')


if __name__ == '__main__':
    main()
