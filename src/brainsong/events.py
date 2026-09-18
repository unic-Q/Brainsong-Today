import calendar
import re
from datetime import date, timedelta
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .model import digest


def month_before(day):
    year, month = (day.year-1, 12) if day.month == 1 else (day.year, day.month-1)
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def date_mentioned(day, quote):
    if str(day.year) not in quote:
        return False
    patterns = [rf"{day.year}[-/.年]\s*0?{day.month}[-/.月]\s*0?{day.day}(?:日|\b)",
                rf"0?{day.month}月\s*0?{day.day}(?:日|[—–至\-])"]
    names = [calendar.month_name[day.month], calendar.month_abbr[day.month]]
    for name in names:
        patterns.extend([rf"\b{day.day}(?:st|nd|rd|th)?(?:\s*[-–—]\s*\d{{1,2}})?\s+{name}\b",
                         rf"\b{name}\s+{day.day}(?:st|nd|rd|th)?\b"])
    return any(re.search(pattern, quote, re.I) for pattern in patterns)


def verify_events(events, today, provider, reader, state, offline=False):
    visible = []
    for event in events:
        key = "event:" + digest(event["name"] + event["kind"] + event["date"])
        old = state.get(key, {})
        effective = dict(event, **old.get("confirmed", {}))
        target = date.fromisoformat(effective["date"])
        if target < today:
            continue
        due = month_before(target)
        checked = old.get("checked", "")
        # Due immediately if added late. Recheck on entry to the 14-day display window,
        # and weekly thereafter; failed confirmations retry next daily run.
        needs = (today >= due and (not old.get("confirmed") or not checked
                 or (today-date.fromisoformat(checked)).days >= 7
                 or (target-today).days <= 14 < (target-date.fromisoformat(checked)).days))
        if needs and not offline:
            try:
                domain = urlsplit(event["url"]).hostname
                rows = provider.search(f"{event['name']} {target.year} {event['kind']} 时间 地点", domain, "noLimit")
                urls = [r.get("link", "") for r in rows] + [event["url"]]
                documents = []
                for url in dict.fromkeys(urls):
                    host = urlsplit(url).hostname or ""
                    if host != domain and not host.endswith("." + domain):
                        continue
                    try:
                        soup = BeautifulSoup(reader.get(url), "html.parser")
                        for node in soup.select("script,style,nav,footer"):
                            node.decompose()
                        text = soup.get_text(" ", strip=True)
                        documents.append({"url": url, "text": text[:14000]})
                    except Exception as exc:
                        state.error(today, "展会官网读取", exc)
                    if len(documents) >= 2:
                        break
                if not documents:
                    raise ValueError("无官方证据")
                result = provider.chat('核对指定展会及指定节点，不把开展日期当报名截止。仅依据官网材料。没有明确证据返回 {"verified":false}。有证据返回 {"verified":true,"date":"YYYY-MM-DD","place":"地点","url":"输入URL","quote":"含年份和节点日期及地点的连续原文引文"}。不猜测，不编造。',
                                       {"event": event, "documents": documents})
                if result.get("verified") is not True:
                    raise ValueError("官网未确认节点")
                document = next((d for d in documents if d["url"] == result.get("url")), None)
                quote = result.get("quote", "")
                verified_day = date.fromisoformat(result["date"])
                if not document or not isinstance(quote, str) or len(quote) < 15 or quote not in document["text"]:
                    raise ValueError("引文无法核对")
                # Numeric date evidence required; unsupported formats stay unconfirmed.
                if not date_mentioned(verified_day, quote) or not result.get("place") or result["place"] not in quote:
                    raise ValueError("日期地点证据不足")
                confirmed = {"date": result["date"], "place": result["place"], "url": result["url"]}
                state.put(key, {"checked": today.isoformat(), "confirmed": confirmed, "quote": quote})
                effective.update(confirmed)
                target = verified_day
                old = {"confirmed": confirmed}
            except Exception as exc:
                state.error(today, "展会核实", exc)
                # Do not publish unconfirmed or now-stale entries after a failed recheck.
                continue
        if old.get("confirmed") and today <= target <= today+timedelta(days=14):
            visible.append(effective)
    unique = {}
    for item in sorted(visible, key=lambda e: e["date"]):
        unique.setdefault((item["name"], item["kind"], item["date"]), item)
    return list(unique.values())[:3]
