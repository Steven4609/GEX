"""GEX 小报 · GitHub 版主脚本

做什么（每次运行）：
  1. 用纽交所日历判断模式：盘前 pre / 盘中 mid / 盘后 post，以及几率算到哪天收盘（horizon）。
  2. 抓 SPY、QQQ 和自选股的期权链：先用 CBOE 延迟行情；CBOE 不通时改用 Yahoo Finance（gamma 用 Black-Scholes 自算）。
  3. 调用 compute.mjs 计算（算法与 Claude 定时任务完全一致）。
  4. 调用 events.py 抓事件（经济数据、联储讲话、大型科技股财报、异动股新闻）。
  5. 写 data/latest.json、data/reports/<时间>.json，更新 data/tracking.csv（盘后先回填当天到期的行）。
  6. 清理 30 天前的报告文件。

用法：
  python scripts/run.py --scheduled          定时任务：只在交易日 17:45 ET 之后、且今天还没出过盘后报告时运行
  python scripts/run.py                      手动：立即按当前时间运行
  python scripts/run.py --tickers "NVDA,TSLA" 同时把自选股清单改成这些代码（写入 config/watchlist.txt）
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORTS = DATA / "reports"
WATCH = ROOT / "config" / "watchlist.txt"
TRACK = DATA / "tracking.csv"
ET = ZoneInfo("America/New_York")
CORE = [("SPY", "SPXL", "SPXS"), ("QQQ", "TQQQ", "SQQQ")]
MAX_WATCH = 12
KEEP_DAYS = 30


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- calendar
def post_done(session: str) -> bool:
    """这个交易日的盘后报告是否已经生成（按报告里的 session 字段判断，延迟到次日凌晨生成的也算）。"""
    for f in sorted(REPORTS.glob("*-post.json"), reverse=True)[:15]:
        try:
            if json.loads(f.read_text(encoding="utf-8")).get("session") == session:
                return True
        except Exception:
            pass
    return False


def decide(now: pd.Timestamp, scheduled: bool):
    import exchange_calendars as xc

    cal = xc.get_calendar("XNYS")
    d = now.normalize().tz_localize(None)
    out = {"et_now": now.strftime("%Y-%m-%d %H:%M")}
    if scheduled:
        # 定时任务：找最近一个已经收盘的交易日。收盘后 1 小时 45 分到 14 小时之间、且它的盘后报告还没生成时才做。
        # GitHub 的定时任务常常延迟几个小时，甚至拖到第二天凌晨，这样延迟了也能补上当天的盘后报告。
        last = cal.date_to_session(d, direction="previous")
        if cal.session_close(last) > now:
            last = cal.previous_session(last)
        close = cal.session_close(last).tz_convert(ET)
        since = (now - close).total_seconds() / 3600
        out.update(mode="post", session=last.strftime("%Y-%m-%d"), horizon=cal.next_session(last).strftime("%Y-%m-%d"))
        if since < 1.75:
            return {**out, "action": "skip", "reason": f"还没到盘后时段（{now:%H:%M} ET，收盘后 {since:.1f} 小时）"}
        if since > 14:
            return {**out, "action": "skip", "reason": f"不在盘后时段（{now:%H:%M} ET，离上个交易日收盘已 {since:.0f} 小时）"}
        if post_done(out["session"]):
            return {**out, "action": "skip", "reason": f"{out['session']} 的盘后报告已生成"}
        out["action"] = "run"
    elif not cal.is_session(d):
        # manual run on a non-trading day: treat as after the last session's close
        last = cal.date_to_session(d, direction="previous")
        out.update(action="run", mode="post", horizon=cal.date_to_session(d, direction="next").strftime("%Y-%m-%d"), session=last.strftime("%Y-%m-%d"))
        close = cal.session_close(last).tz_convert(ET)
    else:
        close = cal.session_close(d).tz_convert(ET)
        h = now.hour + now.minute / 60
        if h < 9.5:
            mode, hz = "pre", d.strftime("%Y-%m-%d")
        elif now < close:
            mode, hz = "mid", d.strftime("%Y-%m-%d")
        else:
            mode, hz = "post", cal.next_session(d).strftime("%Y-%m-%d")
        out.update(action="run", mode=mode, horizon=hz, session=d.strftime("%Y-%m-%d"))
    hzs = pd.Timestamp(out["horizon"])
    out["close"] = close.strftime("%H:%M")
    out["hz_len"] = int((cal.session_close(hzs) - cal.session_open(hzs)).total_seconds() // 60)
    cn = []
    for k in range(1, 8):
        day = d + pd.Timedelta(days=k)
        if day.weekday() < 5 and not cal.is_session(day):
            cn.append({"date": day.strftime("%Y-%m-%d"), "kind": "holiday"})
        elif cal.is_session(day):
            c2 = cal.session_close(day).tz_convert(ET)
            if c2.hour < 16:
                cn.append({"date": day.strftime("%Y-%m-%d"), "kind": "early", "close": c2.strftime("%H:%M")})
    out["cal_note"] = cn
    return out


# ---------------------------------------------------------------- data sources
_session = None


def http():
    global _session
    if _session is None:
        try:
            from curl_cffi import requests as creq

            _session = creq.Session(impersonate="chrome")
        except Exception:  # pragma: no cover
            import requests

            _session = requests.Session()
            _session.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    return _session


def cboe_sym(t: str) -> str:
    return "_" + t if t in {"SPX", "NDX", "RUT", "VIX", "XSP", "DJX"} else t


def cboe_get(path: str):
    url = "https://cdn.cboe.com/api/global/delayed_quotes/" + path
    r = http().get(url, timeout=60, headers={"Referer": "https://www.cboe.com/", "Accept": "application/json,text/plain,*/*"})
    if r.status_code != 200:
        raise RuntimeError(f"CBOE {path} HTTP {r.status_code}")
    return r.json()


OPT_RE = re.compile(r"^([A-Z]+?)(\d{6})([CP])(\d{8})$")


def norm_cboe(t: str):
    j = cboe_get(f"options/{cboe_sym(t)}.json")
    d = j["data"]
    base = t.replace(".", "").replace("-", "")
    roots = {base, base + "W"} if t in {"SPX", "NDX", "RUT", "VIX", "XSP", "DJX"} else {base}
    opts = []
    for o in d.get("options", []):
        m = OPT_RE.match(o.get("option", ""))
        # 只收标准合约：代码根必须等于标的本身（指数另收周度 W 系列），和 Claude 版一致，排除调整后合约（如 B1SPY…、NVDA1…）
        if not m or m.group(1) not in roots:
            continue
        opts.append({"option": "X" + m.group(2) + m.group(3) + m.group(4), "open_interest": o.get("open_interest") or 0, "iv": o.get("iv") or 0, "gamma": o.get("gamma") or 0})
    data = {k: d.get(k) for k in ("close", "current_price", "prev_day_close", "price_change_percent", "high", "low", "last_trade_time")}
    data["options"] = opts
    hist = None
    try:
        h = cboe_get(f"charts/historical/{cboe_sym(t)}.json")
        hist = [float(x["close"]) for x in h["data"]][-260:]
    except Exception as e:
        log(f"  {t} 历史价格取不到（CBOE）：{e}")
    return {"src": "cboe", "root": "X", "timestamp": j.get("timestamp"), "data": data}, hist


def bs_gamma(S, K, T, iv, r=0.04):
    if iv <= 0 or T <= 0 or S <= 0:
        return 0.0
    v = iv * math.sqrt(T)
    d1 = (math.log(S / K) + (r + iv * iv / 2) * T) / v
    return math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi) / (S * v)


def norm_yahoo(t: str, now: datetime):
    import yfinance as yf

    ysym = "^" + t if t in {"SPX", "NDX", "RUT", "VIX"} else t
    tk = yf.Ticker(ysym)
    h = tk.history(period="1y", auto_adjust=False)
    if h.empty:
        raise RuntimeError(f"Yahoo 没有 {t} 的行情")
    closes = [float(x) for x in h["Close"].tolist()]
    last_day = h.index[-1].date()
    today = now.date()
    fi = tk.fast_info
    cur = float(fi.get("lastPrice") or closes[-1])
    close, prev = closes[-1], closes[-2]
    hi, lo = float(h["High"].iloc[-1]), float(h["Low"].iloc[-1])
    ltt = f"{last_day}T16:00:00"
    S = cur or close
    opts = []
    for exp in tk.options:
        ex = datetime.strptime(exp, "%Y-%m-%d").replace(hour=16, tzinfo=ET)
        T = (ex - now).total_seconds() / 86400 / 365
        if T <= 0:
            continue
        try:
            ch = tk.option_chain(exp)
        except Exception as e:
            log(f"  {t} {exp} 期权链取不到：{e}")
            continue
        yy = ex.strftime("%y%m%d")
        for cp, df in (("C", ch.calls), ("P", ch.puts)):
            for _, row in df.iterrows():
                oi = row.get("openInterest")
                iv = row.get("impliedVolatility")
                K = float(row["strike"])
                if not oi or pd.isna(oi) or not iv or pd.isna(iv) or iv < 0.01 or iv > 5:
                    continue
                opts.append({"option": f"X{yy}{cp}{int(round(K * 1000)):08d}", "open_interest": int(oi), "iv": float(iv), "gamma": bs_gamma(S, K, T, float(iv))})
        time.sleep(0.2)
    base = prev if last_day == today else close
    data = {"close": close, "current_price": cur, "prev_day_close": prev if last_day == today else close, "price_change_percent": (cur / base - 1) * 100 if base else None,
            "high": hi, "low": lo, "last_trade_time": ltt, "options": opts}
    return {"src": "yahoo", "root": "X", "timestamp": now.strftime("%Y-%m-%d %H:%M:%S ET"), "data": data}, closes[-260:]


def quote_only(t: str, now: datetime):
    """Close / prev close / last trade time for a leveraged ETF."""
    try:
        j = cboe_get(f"options/{t}.json")
        d = j["data"]
        return {k: d.get(k) for k in ("close", "prev_day_close", "last_trade_time", "current_price")}, "cboe"
    except Exception as e:
        log(f"  {t} CBOE 报价取不到：{e}，改用 Yahoo")
    import yfinance as yf

    h = yf.Ticker(t).history(period="5d", auto_adjust=False)
    closes = h["Close"].tolist()
    last_day = h.index[-1].date()
    return {"close": float(closes[-1]), "prev_day_close": float(closes[-2]), "last_trade_time": f"{last_day}T16:00:00"}, "yahoo"


def fetch_ticker(t: str, now: datetime, prefer: str):
    errs = []
    order = ["cboe", "yahoo"] if prefer != "yahoo" else ["yahoo", "cboe"]
    for src in order:
        try:
            raw, hist = norm_cboe(t) if src == "cboe" else norm_yahoo(t, now)
            n = sum(1 for o in raw["data"]["options"] if o["open_interest"] > 0)
            if n == 0:
                raise RuntimeError("期权链为空")
            if hist is None and src == "cboe":
                try:
                    _, hist = norm_yahoo_hist(t)
                except Exception:
                    pass
            log(f"  {t}: {src}，{n} 个有未平仓量的合约")
            return raw, hist, errs
        except Exception as e:
            errs.append(f"{src}: {e}")
            log(f"  {t} {src} 失败：{e}")
    raise RuntimeError("；".join(errs))


def norm_yahoo_hist(t):
    import yfinance as yf

    h = yf.Ticker(t).history(period="1y", auto_adjust=False)
    return None, [float(x) for x in h["Close"].tolist()][-260:]


# ---------------------------------------------------------------- watchlist
def read_watchlist():
    if not WATCH.exists():
        return []
    out = []
    for line in WATCH.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().upper()
        if line and re.fullmatch(r"[A-Z][A-Z.\-]{0,9}", line) and line not in out:
            out.append(line)
    return out


def write_watchlist(spec: str):
    ticks = []
    for x in re.split(r"[\s,，;；]+", spec.upper()):
        x = x.strip().lstrip("$^")
        if x and re.fullmatch(r"[A-Z][A-Z.\-]{0,9}", x) and x not in ticks and x not in ("SPY", "QQQ"):
            ticks.append(x)
    ticks = ticks[:MAX_WATCH]
    WATCH.parent.mkdir(parents=True, exist_ok=True)
    WATCH.write_text("# 自选股代码，每行一个（最多 %d 个）。网页上的“更新”按钮会改写这个文件。\n" % MAX_WATCH + "\n".join(ticks) + "\n", encoding="utf-8")
    return ticks


# ---------------------------------------------------------------- tracking
TRACK_COLS = ["报告日期 date", "时段 session", "标的 ticker", "方向 dir", "价位 level", "类型 type", "报告时价格 price",
              "预测碰到 p_touch", "预测收在外侧 p_close", "评估日 eval_date", "实际碰到 touched", "实际收在外侧 closed_beyond",
              "评估日收盘 eval_close", "报告时日高 rep_high", "报告时日低 rep_low", "数据源 src"]


def track_read():
    if not TRACK.exists():
        return []
    with TRACK.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))[1:]


def track_write(rows):
    with TRACK.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(TRACK_COLS)
        w.writerows(rows)


def track_eval(rows, state, today):
    """Post-market: fill rows whose eval date is today (same rules as the Claude task's track.py)."""
    n = 0
    for r in rows:
        if r[9] != today or r[10] != "待定":
            continue
        s = state.get(r[2])
        if not s:
            continue
        H, L, C, K = s["dayHi"], s["dayLo"], s["dayClose"], float(r[4])
        rep_hi = float(r[13]) if r[13] not in ("", None) else None
        rep_lo = float(r[14]) if r[14] not in ("", None) else None
        if r[3] == "跌破":
            if rep_lo is not None and rep_lo <= K:
                touched = "是" if L < rep_lo else "不确定"
            else:
                touched = "是" if L <= K else "否"
            beyond = "是" if C < K else "否"
        else:
            if rep_hi is not None and rep_hi >= K:
                touched = "是" if H > rep_hi else "不确定"
            else:
                touched = "是" if H >= K else "否"
            beyond = "是" if C > K else "否"
        r[10], r[11], r[12] = touched, beyond, round(C, 2)
        n += 1
    return n


def track_new(state, results):
    seg = {"pre": "盘前", "mid": "盘中 " + state["time"], "post": "盘后"}[state["mode"]]
    src = {a["t"]: a["src"] for a in results}
    out = []
    for a in results:
        s = state[a["t"]]
        for l in s["lv"]:
            out.append([state["date"], seg, a["t"], "升穿" if l["dir"] == "up" else "跌破", l["K"], l["type"], s["S"],
                        f"{round(l['touch'] * 100)}%", f"{round(l['close'] * 100)}%", state["horizon"], "待定", "待定", "",
                        "" if s["hi"] is None else s["hi"], "" if s["lo"] is None else s["lo"], src[a["t"]]])
    return out


def track_dedupe(rows):
    """频繁手动更新会让同一标的在短时间内重复记很多行，把胜率统计拉偏。
    规则：同一天、同一标的、同一时段类型（盘前 / 盘中 / 盘后）的报告，60 分钟内只保留最后一份的行。"""
    def minute(seg):
        m = re.search(r"(\d{1,2}):(\d{2})", seg)
        return int(m.group(1)) * 60 + int(m.group(2)) if m else None
    groups = {}
    for i, r in enumerate(rows):
        typ = r[1].split(" ")[0]
        groups.setdefault((r[0], typ, r[2]), {}).setdefault(r[1], []).append(i)
    drop = set()
    for g in groups.values():
        segs = sorted(g, key=lambda x: minute(x) if minute(x) is not None else -1)
        keep_t = None
        for seg in reversed(segs):  # newest first
            t = minute(seg)
            if keep_t is None or t is None or keep_t - t >= 60:
                keep_t = t if t is not None else keep_t
                if t is None and keep_t is not None and seg != segs[-1]:
                    drop.update(g[seg]); continue
                continue
            drop.update(g[seg])
    out, seen = [], {}
    for i, r in enumerate(rows):  # 同一份报告重复写入的完全相同的行，只留最后一次
        if i in drop:
            continue
        k = tuple(r[:6])
        if k in seen:
            out[seen[k]] = None
        seen[k] = len(out)
        out.append(r)
    return [r for r in out if r is not None]


def track_summary(rows):
    """Hit rate vs. predicted average, by type and regime-free; shown on the page."""
    groups = {}
    for r in rows:
        if r[10] not in ("是", "否"):
            continue
        for key in ("all", r[5], r[2]):
            g = groups.setdefault(key, {"n": 0, "hit": 0, "pred": 0.0, "nb": 0, "beyond": 0, "predb": 0.0})
            g["n"] += 1
            g["hit"] += r[10] == "是"
            g["pred"] += float(r[7].rstrip("%")) / 100
            if r[11] in ("是", "否"):
                g["nb"] += 1
                g["beyond"] += r[11] == "是"
                g["predb"] += float(r[8].rstrip("%")) / 100
    return {k: {"n": g["n"], "hit": round(g["hit"] / g["n"], 3), "pred": round(g["pred"] / g["n"], 3),
                "beyond": round(g["beyond"] / g["nb"], 3) if g["nb"] else None, "predBeyond": round(g["predb"] / g["nb"], 3) if g["nb"] else None}
            for k, g in groups.items()}


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scheduled", action="store_true")
    ap.add_argument("--tickers", default=None, help="新的自选股清单（逗号或空格分隔）；不填则沿用 config/watchlist.txt")
    ap.add_argument("--source", default=os.environ.get("GEX_SOURCE", "cboe"), choices=["cboe", "yahoo"])
    ap.add_argument("--no-events", action="store_true")
    args = ap.parse_args()

    now = pd.Timestamp(os.environ["GEX_NOW"], tz=ET) if os.environ.get("GEX_NOW") else pd.Timestamp.now(tz=ET)  # GEX_NOW 只用于测试
    dec = decide(now, args.scheduled)
    log(json.dumps(dec, ensure_ascii=False))
    if dec["action"] != "run":
        log("跳过：" + dec["reason"])
        return 0

    spec = (args.tickers or "").strip()
    if spec.lower() in ("none", "-", "clear"):
        watch = write_watchlist("")  # 网页上清空了自选股
    elif spec:
        watch = write_watchlist(spec)
    else:
        watch = read_watchlist()
    log("自选股：" + (", ".join(watch) or "（无）"))

    nowdt = now.to_pydatetime()
    tickers, fetch_errors, sources = [], [], {}
    for t, bull, bear in CORE + [(w, None, None) for w in watch]:
        try:
            raw, hist, errs = fetch_ticker(t, nowdt, args.source)
            sources[t] = raw["src"]
            if errs:
                fetch_errors.append({"t": t, "error": "；".join(errs), "recovered": True})
            etf = {}
            if bull:
                etf["bull"], _ = quote_only(bull, nowdt)
                etf["bear"], _ = quote_only(bear, nowdt)
            tickers.append({"t": t, "bull": bull, "bear": bear, "raw": raw, "hist": hist, "etf": etf})
        except Exception as e:
            fetch_errors.append({"t": t, "error": str(e), "recovered": False})
    if not any(x["t"] in ("SPY", "QQQ") for x in tickers):
        log("SPY 和 QQQ 都取不到数据，本次不更新")
        (DATA / "last_error.json").write_text(json.dumps({"time": dec["et_now"], "errors": fetch_errors}, ensure_ascii=False, indent=1), encoding="utf-8")
        return 1

    prev = None
    latest = DATA / "latest.json"
    if latest.exists():
        try:
            prev = json.loads(latest.read_text(encoding="utf-8")).get("state")
        except Exception:
            prev = None

    # 盘后报告如果是在交易日之后才算的（定时任务拖到第二天凌晨、或周末 / 假日手动运行），
    # 计算时把“当前时间”定在该交易日的 23:59，几率和期权剩余天数才和当晚算的一致
    cnow = now
    if dec["mode"] == "post" and dec["session"] != now.strftime("%Y-%m-%d"):
        cnow = pd.Timestamp(dec["session"] + " 23:59", tz=ET)
    run = {"mode": dec["mode"], "horizon": dec["horizon"], "close": dec["close"], "hzLen": dec["hz_len"], "session": dec["session"],
           "now": {"y": cnow.year, "m": cnow.month, "d": cnow.day, "H": cnow.hour, "M": cnow.minute}}
    p = subprocess.run(["node", str(ROOT / "scripts" / "compute.mjs")], input=json.dumps({"run": run, "tickers": tickers}),
                       capture_output=True, text=True, timeout=600)
    if p.returncode != 0:
        log(p.stderr)
        raise SystemExit("compute.mjs 出错")
    res = json.loads(p.stdout)
    state = res["state"]
    state["label_mode"] = dec["mode"]

    events, ev_err, watch_news = None, None, None
    if not args.no_events:
        try:
            sys.path.insert(0, str(ROOT / "scripts"))
            import events as evmod

            events = evmod.collect(now=now, mode=dec["mode"], session=dec["session"], horizon=dec["horizon"], watch=watch)
            if watch:
                try:
                    watch_news = evmod.watch_news(watch, now)
                except Exception as e:
                    log("自选股新闻抓取失败：" + str(e))
        except Exception as e:
            ev_err = str(e)
            log("事件抓取失败：" + ev_err)

    # tracking
    rows = track_read()
    filled = track_eval(rows, state, dec["session"]) if dec["mode"] == "post" else 0
    new = track_new(state, res["results"])
    # 同一分钟重复运行：先去掉同一报告标签的旧行
    keys = {(r[0], r[1], r[2]) for r in new}
    rows = [r for r in rows if (r[0], r[1], r[2]) not in keys] + new
    rows = track_dedupe(rows)
    track_write(rows)

    report = {
        "generated": now.strftime("%Y-%m-%d %H:%M ET"),
        "generated_utc": pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ"),
        "trigger": "scheduled" if args.scheduled else "manual",
        "mode": dec["mode"], "session": dec["session"], "horizon": dec["horizon"], "close": dec["close"],
        "calNote": dec["cal_note"], "isOpex": res["isOpex"],
        "core": [a for a in res["results"] if a["t"] in ("SPY", "QQQ")],
        "watch": [a for a in res["results"] if a["t"] not in ("SPY", "QQQ")],
        "watchlist": watch,
        "errors": fetch_errors + [{"t": e["t"], "error": e["error"], "recovered": False} for e in res["errors"]],
        "events": events, "eventsError": ev_err, "watchNews": watch_news,
        "prev": prev, "state": state,
        "tracking": {"filled": filled, "rows": len(rows), "summary": track_summary(rows)},
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    name = f"{now:%Y-%m-%d-%H%M}-{dec['mode']}.json"
    (REPORTS / name).write_text(json.dumps(report, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    latest.write_text(json.dumps(report, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    cutoff = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    for f in REPORTS.glob("*.json"):
        if f.name[:10] < cutoff:
            f.unlink()
    idx = sorted((f.name for f in REPORTS.glob("*.json")), reverse=True)
    (DATA / "index.json").write_text(json.dumps(idx), encoding="utf-8")
    (DATA / "last_error.json").unlink(missing_ok=True)
    log(f"完成：{name}；回填 {filled} 行；跟踪表共 {len(rows)} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
