"""测试 GitHub 服务器能否连上各数据源（只读，不写文件）。"""
import time

from curl_cffi import requests as creq
import requests

URLS = [
    ("CBOE options SPY", "https://cdn.cboe.com/api/global/delayed_quotes/options/SPY.json", {"Referer": "https://www.cboe.com/"}),
    ("CBOE historical SPY", "https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/SPY.json", {"Referer": "https://www.cboe.com/"}),
    ("Nasdaq economic", "https://api.nasdaq.com/api/calendar/economicevents?date=2026-11-20", {"Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}),
    ("Nasdaq earnings", "https://api.nasdaq.com/api/calendar/earnings?date=2026-11-19", {"Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}),
    ("Nasdaq quote", "https://api.nasdaq.com/api/quote/NVDA/info?assetclass=stocks", {"Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}),
    ("ForexFactory", "https://nfs.faireconomy.media/ff_calendar_thisweek.json", {}),
    ("Google News RSS", "https://news.google.com/rss/search?q=NVDA%20stock%20(site:cnbc.com%20OR%20site:bloomberg.com%20OR%20site:reuters.com)%20when:3d&hl=en-US&gl=US&ceid=US:en", {}),
    ("Nasdaq news", "https://api.nasdaq.com/api/news/topic/articlebysymbol?q=NVDA|stocks&offset=0&limit=5", {"Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}),
    ("CNBC RSS", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839069", {}),
    ("Yahoo RSS", "https://feeds.finance.yahoo.com/rss/2.0/headline?s=NVDA&region=US&lang=en-US", {}),
]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
for client in ("curl_cffi", "requests"):
    s = creq.Session(impersonate="chrome") if client == "curl_cffi" else requests.Session()
    if client == "requests":
        s.headers["User-Agent"] = UA
    for name, url, h in URLS:
        t = time.time()
        try:
            r = s.get(url, headers=h, timeout=30)
            body = r.text[:80].replace("\n", " ")
            print(f"{client:9s} {name:20s} {r.status_code}  {len(r.content):>9d} bytes  {time.time() - t:5.1f}s  {body}")
        except Exception as e:
            print(f"{client:9s} {name:20s} ERROR {e}")
        time.sleep(1)

import yfinance as yf

try:
    tk = yf.Ticker("SPY")
    exps = tk.options
    ch = tk.option_chain(exps[0])
    print(f"yfinance  SPY options: {len(exps)} expiries, first chain {len(ch.calls)} calls, OI sum {int(ch.calls.openInterest.fillna(0).sum())}")
    print(f"yfinance  SPY history rows: {len(tk.history(period='1y'))}")
except Exception as e:
    print("yfinance  ERROR", e)
