"""
F&O News Alert Bot
-------------------
Pulls NSE corporate announcements + Google News RSS + a few financial RSS
feeds, scores them for materiality against your F&O watchlist, dedups
against previously-seen items, and pushes a digest to Telegram.

Run this on a schedule (see .github/workflows/alert.yml) via GitHub Actions.
No paid APIs required.
"""

import os
import re
import json
import time
import calendar
import hashlib
import requests
import feedparser
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SEEN_FILE = "data/seen_ids.json"
SEEN_RETENTION_HOURS = 72  # trim old entries so the file doesn't grow forever

# Ignore any news item older than this base value; the actual window used
# each run adjusts automatically based on the gap since the last run (see
# get_recency_window_hours below) — a 90-min-interval run needs a small
# window, but the evening run after a long gap needs a bigger one.
RECENCY_WINDOW_HOURS = 2.5

IST = timezone(timedelta(hours=5, minutes=30))

# Anything matching these is thrown out immediately, no matter the score.
# This is what was letting IPO/listing news slip in.
HARD_EXCLUDE_KEYWORDS = [
    "ipo", "initial public offering", "grey market premium", " gmp ",
    "anchor investor", "drhp", "draft red herring", "listing gain",
    "subscription status", "price band",
]

# Fallback static list if the NSE F&O CSV fetch fails.
# Replace/extend as needed. Full official list changes quarterly.
FALLBACK_FO_SYMBOLS = {
    "RELIANCE": "Reliance Industries", "TCS": "Tata Consultancy Services",
    "INFY": "Infosys", "HDFCBANK": "HDFC Bank", "ICICIBANK": "ICICI Bank",
    "KEIIND": "KEI Industries", "POLYCAB": "Polycab India",
    "HAVELLS": "Havells India", "ULTRACEMCO": "UltraTech Cement",
    "TATASTEEL": "Tata Steel", "TATAMOTORS": "Tata Motors",
    "SBIN": "State Bank of India", "AXISBANK": "Axis Bank",
    "MARUTI": "Maruti Suzuki", "SUNPHARMA": "Sun Pharma",
    "BAJFINANCE": "Bajaj Finance", "ADANIENT": "Adani Enterprises",
    "WIPRO": "Wipro", "HCLTECH": "HCL Technologies", "LT": "Larsen & Toubro",
    # ... extend this or rely on the dynamic fetch below
}

# ---------------------------------------------------------------------------
# CATALYST LIBRARY
# This is the actual "what moves a stock" blueprint — organized by category
# so it's easy to extend. Each entry: keyword -> materiality weight.
# Built from patterns that have repeatedly moved Indian F&O stocks over the
# last decade (management shocks, capacity/competitive shifts, order wins,
# fraud/regulatory action, M&A, financial surprises) plus a forward-looking
# set of emerging catalysts to watch for as they become common triggers.
# ---------------------------------------------------------------------------

CATALYST_CATEGORIES = {
    # Leadership & governance shocks
    "management_governance": {
        "ceo resignation": 6, "ceo steps down": 6, "cfo resignation": 5,
        "md resignation": 6, "founder exit": 5, "promoter stake sale": 5,
        "whistleblower": 6, "auditor resignation": 6, "fraud": 6,
        "related party transaction": 4, "insider trading": 5,
        "raid": 5, "investigation": 5, "sebi action": 5, "regulatory action": 5,
        "rating downgrade": 5, "rating upgrade": 3,
    },
    # Capacity & competitive landscape shifts (the KEI/Ultratech pattern)
    "capacity_competition": {
        "capacity expansion": 4, "new plant": 4, "greenfield": 4,
        "brownfield": 3, "enters the market": 4, "to foray into": 4,
        "second largest": 3, "market share": 3, "capex guidance": 3,
        "backward integration": 3, "forward integration": 3,
        "new competitor": 4, "aggressive expansion": 4,
    },
    # Orders, tenders, contracts
    "orders_contracts": {
        "order win": 5, "bags order": 5, "tender": 4, "contract win": 5,
        "wins contract": 5, "export order": 4, "loi": 3,
        "letter of intent": 3, "loses contract": 5, "client exit": 4,
        "government order": 4, "defence order": 4, "large order": 4,
    },
    # M&A / corporate structure
    "ma_corporate_structure": {
        "acquisition": 5, "merger": 5, "stake sale": 4, "stake purchase": 4,
        "demerger": 4, "spin-off": 4, "delisting": 5, "qip": 3,
        "preferential allotment": 3, "rights issue": 3, "buyback": 3,
        "bonus": 3, "stock split": 3, "open offer": 4,
    },
    # Financial performance surprises
    "financial_surprises": {
        "profit warning": 6, "guidance cut": 6, "guidance raise": 5,
        "margin pressure": 4, "impairment": 5, "provisioning": 4,
        "debt restructuring": 5, "default": 6, "beats estimates": 4,
        "misses estimates": 4,
    },
    # Technology & sector innovation triggers
    "technology_innovation": {
        "patent": 3, "breakthrough": 3, "partnership with": 3,
        "joint venture": 4, "technology tie-up": 3, "product launch": 3,
        "product recall": 5, "ev foray": 3, "semiconductor": 3,
        "battery technology": 3, "ai partnership": 3,
    },
    # Forward-looking / emerging catalysts to keep watching for
    "emerging_watchlist": {
        "data center": 3, "green hydrogen": 3, "pli scheme": 4,
        "china plus one": 3, "defence indigenisation": 3,
        "semiconductor fab": 4, "carbon tax": 3, "import duty": 4,
        "export duty": 4, "customs duty": 4, "production linked incentive": 4,
    },
}

# Flatten into one lookup for scoring, category kept for reference/tuning
HIGH_WEIGHT_KEYWORDS = {
    kw: weight
    for category in CATALYST_CATEGORIES.values()
    for kw, weight in category.items()
}

LOW_WEIGHT_NOISE = [
    "block deal", "bulk deal",  # often noise without context
    "technical view", "brokerage view", "target price",  # analyst chatter
]

SOURCE_TIER_WEIGHT = {
    "NSE": 5,
    "GoogleNews": 3,
    "RSS": 3,
}

MATERIALITY_ALERT_THRESHOLD = 6  # tune based on backtesting

# A few direct RSS feeds as backup/supplement to Google News
STATIC_RSS_FEEDS = [
    ("Moneycontrol", "https://www.moneycontrol.com/rss/marketreports.xml"),
    ("ETMarkets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("BusinessStandard", "https://www.business-standard.com/rss/markets-106.rss"),
]

# Queries built directly from the catalyst categories above — each one
# targets a specific kind of stock-moving event rather than generic market
# chatter. Deliberately excludes IPO-type language.
GOOGLE_NEWS_QUERIES = [
    "NSE listed company CEO OR MD OR CFO resignation",
    "NSE listed company capacity expansion plant",
    "NSE listed company order win OR tender OR contract -IPO",
    "NSE listed company acquisition OR merger OR stake -IPO",
    "NSE listed company credit rating upgrade OR downgrade",
    "NSE listed company SEBI investigation OR fraud OR raid",
    "NSE listed company profit warning OR guidance cut",
    "NSE listed company buyback OR bonus OR stock split",
    "NSE sector new competitor OR foray into market",
    "NSE listed company technology partnership OR patent",
]

# ---------------------------------------------------------------------------
# FETCHERS
# ---------------------------------------------------------------------------

def get_fo_symbols():
    """Fetch the current F&O symbol list from NSE; fall back to static list."""
    url = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        symbols = {}
        for line in r.text.splitlines()[1:]:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2 and parts[1]:
                symbols[parts[1].upper()] = parts[1]
        if symbols:
            return symbols
    except Exception as e:
        print(f"[warn] F&O list fetch failed, using fallback: {e}")
    return FALLBACK_FO_SYMBOLS


def fetch_nse_announcements():
    """
    NSE requires a warm session (cookies from the homepage) before the API
    will respond — hitting the API cold usually 403s. This is an unofficial
    endpoint and NSE changes its anti-bot behavior periodically; if this
    breaks, that's expected and the rest of the pipeline still runs.
    """
    items = []
    try:
        session = requests.Session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept-Language": "en-US,en;q=0.9",
        }
        session.get("https://www.nseindia.com", headers=headers, timeout=10)
        time.sleep(1)
        resp = session.get(
            "https://www.nseindia.com/api/corporate-announcements?index=equities",
            headers=headers, timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        for row in data:
            items.append({
                "source": "NSE",
                "symbol": row.get("symbol", "").upper(),
                "headline": row.get("desc") or row.get("subject", ""),
                "summary": row.get("attchmntText", "") or "",
                "url": row.get("attchmntFile", ""),
                "ts": row.get("an_dt", datetime.now(timezone.utc).isoformat()),
            })
    except Exception as e:
        print(f"[warn] NSE announcements fetch failed: {e}")
    return items


def fetch_google_news(query):
    items = []
    try:
        url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=en-IN&gl=IN&ceid=IN:en"
        feed = feedparser.parse(url)
        for entry in feed.entries:
            items.append({
                "source": "GoogleNews",
                "symbol": None,  # resolved later via text matching
                "headline": entry.get("title", ""),
                "summary": entry.get("summary", ""),
                "url": entry.get("link", ""),
                "ts": entry.get("published", datetime.now(timezone.utc).isoformat()),
            })
    except Exception as e:
        print(f"[warn] Google News fetch failed for '{query}': {e}")
    return items


def fetch_static_rss():
    items = []
    for name, url in STATIC_RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                items.append({
                    "source": "RSS",
                    "symbol": None,
                    "headline": entry.get("title", ""),
                    "summary": entry.get("summary", ""),
                    "url": entry.get("link", ""),
                    "ts": entry.get("published", datetime.now(timezone.utc).isoformat()),
                })
        except Exception as e:
            print(f"[warn] RSS fetch failed for {name}: {e}")
    return items


# ---------------------------------------------------------------------------
# PROCESSING
# ---------------------------------------------------------------------------

def resolve_symbol(item, fo_symbols):
    """If symbol isn't already tagged (NSE items have it), match by company
    name / symbol mention in headline+summary text."""
    if item.get("symbol"):
        return item["symbol"] if item["symbol"] in fo_symbols else None
    text = f"{item['headline']} {item['summary']}".upper()
    for sym, name in fo_symbols.items():
        if sym in text or name.upper() in text:
            return sym
    return None


def is_hard_excluded(item):
    """Kill IPO/listing-type noise outright, regardless of score."""
    text = f"{item['headline']} {item['summary']}".lower()
    return any(kw in text for kw in HARD_EXCLUDE_KEYWORDS)


def score_materiality(item):
    text = f"{item['headline']} {item['summary']}".lower()
    score = SOURCE_TIER_WEIGHT.get(item["source"], 1)
    for kw, weight in HIGH_WEIGHT_KEYWORDS.items():
        if kw in text:
            score += weight
    for noise_kw in LOW_WEIGHT_NOISE:
        if noise_kw in text:
            score -= 3
    return score


def parse_item_datetime(ts_str):
    """
    Try several date formats since NSE, Google News, and RSS feeds all
    write timestamps differently. Returns a timezone-aware datetime, or
    None if it genuinely can't be parsed (caller decides what to do then).
    """
    if not ts_str:
        return None
    # Most RSS/Google News dates are RFC-822 style, e.g. "Thu, 04 Sep 2026 12:00:00 GMT"
    try:
        dt = parsedate_to_datetime(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass
    # Fallback formats sometimes seen from NSE-style feeds
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S"):
        try:
            return datetime.strptime(ts_str, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def get_recency_window_hours(now_ist):
    """
    The alert schedule isn't evenly spaced — most runs are 90 minutes apart
    during market hours, but the evening run (8pm) comes ~5 hours after the
    3pm final report. Widen the window automatically for that gap instead
    of hardcoding one number that's wrong for one of the runs.
    """
    hour = now_ist.hour
    if 18 <= hour <= 21:  # the evening/post-market run
        return 6.0
    return RECENCY_WINDOW_HOURS


def is_recent_enough(item, window_hours):
    """True if the item is within window_hours. If we can't parse the
    timestamp at all, we keep NSE items (exchange filings are almost
    always genuinely fresh) but drop everything else to be safe."""
    dt = parse_item_datetime(item.get("ts"))
    if dt is None:
        return item["source"] == "NSE"
    age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    return -1 <= age_hours <= window_hours  # small negative buffer for clock skew


def item_id(item):
    raw = item.get("url") or f"{item['source']}|{item['headline']}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            data = json.load(f)
    else:
        data = {}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=SEEN_RETENTION_HOURS)
    return {k: v for k, v in data.items() if datetime.fromisoformat(v) > cutoff}


def save_seen(seen):
    os.makedirs("data", exist_ok=True)
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f)


# ---------------------------------------------------------------------------
# DAILY LOG (for the 3pm end-of-day recap)
# ---------------------------------------------------------------------------

DAILY_LOG_FILE = "data/daily_log.json"


def load_daily_log(today_str):
    if os.path.exists(DAILY_LOG_FILE):
        with open(DAILY_LOG_FILE) as f:
            log = json.load(f)
        if log.get("date") == today_str:
            return log
    return {"date": today_str, "alerts": []}  # fresh log for a new day


def save_daily_log(log):
    os.makedirs("data", exist_ok=True)
    with open(DAILY_LOG_FILE, "w") as f:
        json.dump(log, f)


def is_final_report_time(now_ist):
    return now_ist.hour == 15  # the 3pm slot


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Telegram caps messages at 4096 chars — chunk if needed
    for i in range(0, len(text), 4000):
        chunk = text[i:i + 4000]
        resp = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }, timeout=10)
        if resp.status_code != 200:
            print(f"[error] Telegram send failed: {resp.text}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    now_ist = datetime.now(IST)
    today_str = now_ist.strftime("%Y-%m-%d")
    window_hours = get_recency_window_hours(now_ist)
    final_report = is_final_report_time(now_ist)

    fo_symbols = get_fo_symbols()
    print(f"Loaded {len(fo_symbols)} F&O symbols")

    raw_items = []
    raw_items += fetch_nse_announcements()
    for q in GOOGLE_NEWS_QUERIES:
        raw_items += fetch_google_news(q)
    raw_items += fetch_static_rss()
    print(f"Fetched {len(raw_items)} raw items")

    seen = load_seen()
    daily_log = load_daily_log(today_str)
    alerts_by_symbol = {}

    for item in raw_items:
        if is_hard_excluded(item):
            continue
        if not is_recent_enough(item, window_hours):
            continue
        iid = item_id(item)
        if iid in seen:
            continue
        symbol = resolve_symbol(item, fo_symbols)
        if not symbol:
            continue
        score = score_materiality(item)
        seen[iid] = datetime.now(timezone.utc).isoformat()
        if score >= MATERIALITY_ALERT_THRESHOLD:
            alerts_by_symbol.setdefault(symbol, []).append((score, item))
            daily_log["alerts"].append({
                "symbol": symbol, "score": score,
                "headline": item["headline"][:180], "url": item.get("url", ""),
            })

    save_seen(seen)
    save_daily_log(daily_log)

    label = "Final Report (3pm)" if final_report else (
        "Post-Market Update" if 18 <= now_ist.hour <= 21 else "Update"
    )

    if alerts_by_symbol:
        lines = [f"*F&O News {label} — {now_ist.strftime('%d %b %H:%M')}*\n"]
        for symbol, entries in sorted(alerts_by_symbol.items(), key=lambda x: -max(s for s, _ in x[1])):
            entries.sort(key=lambda x: -x[0])
            lines.append(f"\n*{symbol}*")
            for score, item in entries[:3]:
                headline = item["headline"][:180]
                lines.append(f"• (score {score}) {headline}")
                if item.get("url"):
                    lines.append(f"  {item['url']}")
        send_telegram("\n".join(lines))
        print(f"Sent digest for {len(alerts_by_symbol)} symbols.")
    else:
        print("No material alerts this cycle.")

    # At the 3pm slot, additionally send a same-day recap of everything
    # that went out since market open — separate from the "new items only"
    # digest above, so it acts as a wrap-up rather than a duplicate.
    if final_report and daily_log["alerts"]:
        by_symbol = {}
        for a in daily_log["alerts"]:
            by_symbol.setdefault(a["symbol"], []).append(a)
        recap = [f"*📋 Full-Day Recap — {now_ist.strftime('%d %b')}*\n"]
        for symbol, entries in sorted(by_symbol.items(), key=lambda x: -max(e["score"] for e in x[1])):
            recap.append(f"\n*{symbol}* ({len(entries)} item(s))")
            top = sorted(entries, key=lambda e: -e["score"])[:2]
            for e in top:
                recap.append(f"• {e['headline']}")
        send_telegram("\n".join(recap))
        print(f"Sent full-day recap covering {len(by_symbol)} symbols.")


if __name__ == "__main__":
    main()
