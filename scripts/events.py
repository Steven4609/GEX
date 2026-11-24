"""GEX 小报 · 事件栏脚本

规则沿用 Claude 定时任务：只收四类事件，宁缺毋滥。
  1. 美国重要经济数据（已公布的带实际值、预期值、前值）
  2. 美联储：利率决议、纪要、褐皮书，以及官员公开讲话
  3. 大型科技股（AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO）和自选股的财报
  4. 异动股新闻：上面这些股票当天（或盘后）涨跌超过 3% 时，附上 Yahoo Finance 最新一条标题
     —— 没有 AI 判断，所以只用“价格明显异动”来筛选，不收政策类新闻

数据源：Nasdaq 经济日历和财报日历（有实际值、预期值）；取不到时用 ForexFactory 周历（只有预期值）
和 yfinance。任何一个源失败都只是少几条，不影响小报。

python scripts/events.py               单独测试，打印结果
"""
from __future__ import annotations

import json
import re
import sys
import time
import xml.etree.ElementTree as ET_XML
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MEGA = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO"]
MOVE = 3.0  # % move that makes a stock's news worth a line
_s = None


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def http():
    global _s
    if _s is None:
        try:
            from curl_cffi import requests as creq

            _s = creq.Session(impersonate="chrome")
        except Exception:
            import requests

            _s = requests.Session()
            _s.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    return _s


def get_json(url, **kw):
    r = http().get(url, timeout=30, headers={"Accept": "application/json, text/plain, */*", **kw.pop("headers", {})}, **kw)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} {url}")
    return r.json()


NASDAQ_H = {"Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}

# ---------------------------------------------------------------- economic data & Fed
# tier 1 = market movers, tier 2 = worth a line; anything else is ignored
ECON = [
    (None, r"Composite PMI|GDP Sales|PCE Prices|GDP Deflator|Real Consumer Spending"),
    (2, r"GDP Price Index"),
    (1, r"^(Core )?CPI|Consumer Price Index"), (1, r"^(Core )?PPI|Producer Price Index"), (1, r"Nonfarm Payrolls|Non-Farm Payrolls"),
    (1, r"^Unemployment Rate"), (1, r"^(Core )?PCE|PCE Price"), (1, r"^GDP|Gross Domestic Product|Prelim GDP|Advance GDP"),
    (1, r"Interest Rate Decision|Fed Interest Rate|Federal Funds Rate"), (1, r"FOMC (Meeting )?Minutes"), (1, r"FOMC Statement|FOMC Press Conference"),
    (2, r"^Initial Jobless Claims|^Unemployment Claims"), (2, r"^(Core )?Retail Sales"), (2, r"ISM (Manufacturing|Non-Manufacturing|Services) PMI$"),
    (2, r"S&P Global (Manufacturing|Services) PMI|Flash (Manufacturing|Services) PMI"), (2, r"^Housing Starts|^Building Permits"), (2, r"CB Consumer Confidence|^Consumer Confidence"),
    (2, r"Michigan Consumer Sentiment|UoM Consumer Sentiment|Prelim UoM"), (2, r"JOLTS"), (2, r"^(Core )?Durable Goods Orders"),
    (2, r"Average Hourly Earnings"), (2, r"ADP Nonfarm|ADP Non-Farm"), (2, r"Beige Book"), (2, r"Philadelphia Fed Manufacturing Index"),
    (2, r"New Home Sales|Existing Home Sales"), (2, r"Industrial Production"),
]
FED = re.compile(r"(Fed|FOMC).*(Speaks|Testifies|Speech)|Powell|Fed Chair", re.I)


def tier(name):
    if FED.search(name):
        return 1 if re.search(r"Chair|Powell", name) and not re.search(r"Vice Chair for Supervision", name) else 2
    for t, pat in ECON:
        if re.search(pat, name, re.I):
            return t
    return None


def clean(v):
    v = (v or "").replace("&nbsp;", "").strip()
    return v or None


def nasdaq_rows(qday):
    j = get_json(f"https://api.nasdaq.com/api/calendar/economicevents?date={qday}", headers=NASDAQ_H)
    return [r for r in (j.get("data") or {}).get("rows") or [] if r.get("country") == "United States"]


_OFFSET = None


def nasdaq_offset(ff):
    """Nasdaq 经济日历的 date 参数实测会错一天（2026-11 查 D 返回 D-1 的事件）。
    用 ForexFactory 的事件时间对照，判断该加 0 天还是 1 天；ForexFactory 取不到时默认加 1 天。"""
    global _OFFSET
    if _OFFSET is not None:
        return _OFFSET
    best, score = 1, -1
    days = sorted({e["date"] for e in ff})
    if days:
        day = days[0]
        ff_times = {e["time"] for e in ff if e["date"] == day}
        for off in (0, 1):
            q = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=off)).strftime("%Y-%m-%d")
            try:
                nt = {r.get("gmt") for r in nasdaq_rows(q)}
            except Exception:
                continue
            sc = len(ff_times & nt) - len(nt - ff_times) * 0.1
            if sc > score:
                best, score = off, sc
    _OFFSET = best
    log(f"  Nasdaq 经济日历日期偏移：+{best} 天")
    return best


def econ_nasdaq(day, offset=1):
    q = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=offset)).strftime("%Y-%m-%d")
    out = []
    for r in nasdaq_rows(q):
        name = (r.get("eventName") or "").strip()
        tr = tier(name)
        if tr is None:
            continue
        out.append({"kind": "fed" if FED.search(name) else "econ", "date": day, "time": r.get("gmt"), "name": name, "tier": tr,
                    "actual": clean(r.get("actual")), "consensus": clean(r.get("consensus")), "previous": clean(r.get("previous")), "src": "nasdaq"})
    return out


def ff_all():
    out = []
    for feed in ("thisweek", "nextweek"):
        try:
            for r in get_json(f"https://nfs.faireconomy.media/ff_calendar_{feed}.json"):
                if r.get("country") == "USD":
                    dt = datetime.fromisoformat(r["date"]).astimezone(ET)
                    out.append({"date": dt.strftime("%Y-%m-%d"), "time": dt.strftime("%H:%M"), "name": r.get("title", "")})
        except Exception as e:
            log(f"  ForexFactory {feed}: {e}")
    return out


def econ_ff(days):
    out = []
    for feed in ("thisweek", "nextweek"):
        try:
            j = get_json(f"https://nfs.faireconomy.media/ff_calendar_{feed}.json")
        except Exception as e:
            log(f"  ForexFactory {feed}: {e}")
            continue
        for r in j:
            if r.get("country") != "USD":
                continue
            dt = datetime.fromisoformat(r["date"]).astimezone(ET)
            day = dt.strftime("%Y-%m-%d")
            if day not in days:
                continue
            name = r.get("title", "")
            tr = tier(name)
            if tr is None and r.get("impact") == "High":
                tr = 1
            if tr is None:
                continue
            out.append({"kind": "fed" if FED.search(name) else "econ", "date": day, "time": dt.strftime("%H:%M"), "name": name, "tier": tr,
                        "actual": clean(r.get("actual")), "consensus": clean(r.get("forecast")), "previous": clean(r.get("previous")), "src": "forexfactory"})
    return out


# ---------------------------------------------------------------- earnings
def earn_nasdaq(day, names):
    j = get_json(f"https://api.nasdaq.com/api/calendar/earnings?date={day}", headers=NASDAQ_H)
    out = []
    for r in (j.get("data") or {}).get("rows") or []:
        if r.get("symbol") in names:
            tm = r.get("time") or ""
            out.append({"kind": "earn", "date": day, "t": r["symbol"], "when": "pre" if "pre" in tm else "after" if "after" in tm else None,
                        "eps": clean(r.get("eps")), "epsForecast": clean(r.get("epsForecast")), "surprise": clean(r.get("surprise")), "src": "nasdaq"})
    return out


def earn_yf(days, names):
    import yfinance as yf

    out = []
    for t in names:
        try:
            cal = yf.Ticker(t).calendar or {}
            ds = cal.get("Earnings Date") or []
            for d in ds:
                ds_ = str(d)[:10]
                if ds_ in days:
                    out.append({"kind": "earn", "date": ds_, "t": t, "when": None, "eps": None,
                                "epsForecast": str(cal.get("Earnings Average")) if cal.get("Earnings Average") is not None else None, "surprise": None, "src": "yahoo"})
        except Exception as e:
            log(f"  yfinance calendar {t}: {e}")
    return out


# ---------------------------------------------------------------- movers + headlines
def pct(s):
    try:
        return float(str(s).replace("%", "").replace("+", "").replace(",", ""))
    except Exception:
        return None


def moves(names):
    """Day % change and after-hours % change per ticker."""
    out = {}
    for t in names:
        try:
            j = get_json(f"https://api.nasdaq.com/api/quote/{t}/info?assetclass=stocks", headers=NASDAQ_H)
            d = j.get("data") or {}
            p, s = d.get("primaryData") or {}, d.get("secondaryData") or {}
            out[t] = {"chg": pct(p.get("percentageChange")), "ah": pct(s.get("percentageChange")) if s else None}
            time.sleep(0.15)
        except Exception as e:
            log(f"  Nasdaq quote {t}: {e}")
    missing = [t for t in names if t not in out]
    if missing:
        try:
            import yfinance as yf

            for t in missing:
                fi = yf.Ticker(t).fast_info
                lp, pc = fi.get("lastPrice"), fi.get("previousClose")
                if lp and pc:
                    out[t] = {"chg": (lp / pc - 1) * 100, "ah": None}
        except Exception as e:
            log(f"  yfinance quotes: {e}")
    return out


def headline(t, since):
    try:
        r = http().get(f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={t}&region=US&lang=en-US", timeout=20)
        if r.status_code == 200:
            root = ET_XML.fromstring(r.content)
            for it in root.iter("item"):
                pub = parsedate_to_datetime(it.findtext("pubDate"))
                if pub >= since:
                    return {"title": it.findtext("title"), "link": it.findtext("link"), "pub": pub.astimezone(ET).strftime("%m/%d %H:%M"), "src": "Yahoo Finance RSS"}
    except Exception as e:
        log(f"  RSS {t}: {e}")
    try:
        import yfinance as yf

        for n in yf.Ticker(t).news or []:
            c = n.get("content") or n
            title = c.get("title")
            ts = c.get("pubDate") or c.get("providerPublishTime")
            pub = datetime.fromisoformat(ts.replace("Z", "+00:00")) if isinstance(ts, str) else datetime.fromtimestamp(ts, ET) if ts else None
            link = ((c.get("canonicalUrl") or {}).get("url")) or c.get("link")
            if title and pub and pub >= since:
                return {"title": title, "link": link, "pub": pub.astimezone(ET).strftime("%m/%d %H:%M"), "src": (c.get("provider") or {}).get("displayName") or "Yahoo Finance"}
    except Exception as e:
        log(f"  yfinance news {t}: {e}")
    return None


# ---------------------------------------------------------------- main entry
def collect(now, mode, session, horizon, watch):
    """Return {"items": [...], "sources": {...}, "notes": [...]}. Items are structured; the page writes the text."""
    days = [session] + ([horizon] if horizon != session else [])
    if mode in ("pre", "mid"):
        # today's remaining session plus the next trading day
        import exchange_calendars as xc

        nxt = xc.get_calendar("XNYS").next_session(session).strftime("%Y-%m-%d")
        if nxt not in days:
            days.append(nxt)
    names = list(dict.fromkeys(MEGA + list(watch)))
    sources, notes = {}, []

    econ = []
    try:
        ff = [e for e in ff_all() if e["name"] not in ("Bank Holiday",)]
    except Exception:
        ff = []
    off = nasdaq_offset([e for e in ff if e["date"] in days] or ff)
    for day in days:
        try:
            econ += econ_nasdaq(day, off)
            sources["econ"] = "Nasdaq"
        except Exception as e:
            log(f"  Nasdaq economic {day}: {e}")
            sources["econ_err"] = str(e)
    if "econ" not in sources:
        econ = econ_ff(days)
        if econ:
            sources["econ"] = "ForexFactory"
    if mode == "post":  # today's data is already in the price; keep only the next session's
        econ = [e for e in econ if e["date"] != session]
    # dedupe (Nasdaq lists m/m and y/y separately with the same name), keep tier order (today first within a tier), max 5, then sort by time
    seen, pick = set(), []
    for e in sorted(econ, key=lambda e: (e["tier"], e["date"], e["time"] or "")):
        key = (e["date"], e["name"])
        if key in seen:
            continue
        seen.add(key)
        pick.append(e)
    pick = sorted(pick[:5], key=lambda e: (e["date"], e["time"] or ""))

    earn = []
    for day in days:
        try:
            earn += earn_nasdaq(day, set(names))
            sources["earnings"] = "Nasdaq"
        except Exception as e:
            log(f"  Nasdaq earnings {day}: {e}")
    if "earnings" not in sources:
        earn = earn_yf(days, names)
        sources["earnings"] = "Yahoo"
    if mode == "post":  # keep today's after-close reports and the next session's
        earn = [e for e in earn if e["date"] != session or e["when"] in ("after", None)]

    news = []
    mv = moves(names)
    since = now.to_pydatetime() - timedelta(hours=30)
    big = sorted([(t, m) for t, m in mv.items() if max(abs(m.get("chg") or 0), abs(m.get("ah") or 0)) >= MOVE],
                 key=lambda x: -max(abs(x[1].get("chg") or 0), abs(x[1].get("ah") or 0)))
    for t, m in big[:2]:
        h = headline(t, since)
        news.append({"kind": "news", "t": t, "chg": None if m.get("chg") is None else round(m["chg"], 2),
                     "ah": None if m.get("ah") is None else round(m["ah"], 2), **({"headline": h} if h else {})})
    sources["news"] = "Nasdaq quotes + Yahoo Finance RSS"
    for e in pick:
        e.pop("tier", None)
    return {"items": pick + earn + news, "sources": sources, "notes": notes, "days": days}


if __name__ == "__main__":
    import pandas as pd

    now = pd.Timestamp.now(tz=ET)
    print(json.dumps(collect(now, "post", now.strftime("%Y-%m-%d"), (now + pd.Timedelta(days=1)).strftime("%Y-%m-%d"), []), ensure_ascii=False, indent=1))
