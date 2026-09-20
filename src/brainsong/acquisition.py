"""Cheap business-context gate before paid analysis; not a substitute for AI relevance."""
import re
from datetime import date
from dataclasses import replace
from .model import company_keys, source_factor

CORE = re.compile(r'脑机|脑电|神经数据|神经接口|耳周|耳部电极|干电极|生物电|神经反馈|\b(?:eeg|bci|ecog)\b|electroencephal|brain.computer|brain.machine|neural (?:data|signal|interface)|ear.eeg|neurofeedback|dry electrode', re.I)
AUDIO = re.compile(r'耳机|headphones?|headsets?|earbuds?', re.I)
APPLICATION = re.compile(r'教育|课堂|教学|听觉训练|学习|睡眠|情绪|新品|发布|降噪|education|classroom|learning|sleep|emotion|launch|release|noise.cancel', re.I)
EVENT = re.compile(r'发布|新品|研发|融资|收购|并购|任命|聘|更新|研究|产品|平台|数据|设备|传感|launch|release|funding|rais|appoint|sdk|studio|update|data|sensor|research|product', re.I)


def business_context(item, cfg):
    text = item.title + ' ' + (item.source_summary or item.summary)[:2000] + ' ' + item.body[:1000]
    if CORE.search(text):
        return True
    if AUDIO.search(text) and APPLICATION.search(text):
        return True
    if company_keys(item, cfg.get('scoring', {})) and EVENT.search(text):
        return True
    # Exploration requires an explicit adjacent application, never merely a new name.
    return bool(item.exploration and re.search(r'可穿戴|wearable', text, re.I)
                and re.search(r'生物传感|柔性|睡眠|情绪|生理|biosens|flexible|sleep|emotion|physiolog', text, re.I))


def source_priority(item, cfg):
    # Pre-analysis source_kind has not been verified; use domain reputation only.
    return source_factor(replace(item, source_kind='secondary'), cfg.get('scoring', {}))


def date_priority(item):
    try:
        return date.fromisoformat((item.first_reported or item.published)[:10]).toordinal()
    except ValueError:
        return 0


def estimated_relevance(item, cfg):
    """Cheap editable acquisition hints; AI still makes the actual admission decision."""
    text = item.title + ' ' + (item.source_summary or item.summary)[:2000]
    for hint in cfg.get('acquisition', {}).get('priority_hints', []):
        if all(re.search(pattern, text, re.I) for pattern in hint['all']):
            return hint['score']
    return max((r['weight'] for r in item.matches), default=20)
