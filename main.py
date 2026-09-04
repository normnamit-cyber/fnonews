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
import hashlib
import requests
import feedparser
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SEEN_FILE = "data/seen_ids.json"
SEEN_RETENTION_HOURS = 72  # trim old entries so the file doesn't grow forever

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

# Keyword weight table for materiality scoring.
# Tune these over time based on what actually moved stocks for you.
HIGH_WEIGHT_KEYWORDS = {
    "acquisition": 5, "merger": 5, "stake sale": 4, "fraud": 6, "default": 6,
    "investigation": 5, "sebi": 4, "resignation": 4, "ceo": 3, "cfo": 3,
    "board meeting": 3, "capacity expansion": 4, "capex": 3, "order win": 4,
    "contract win": 4, "rating downgrade": 5, "rating upgrade": 3,
    "bonus": 3, "buyback": 3, "dividend": 2, "stock split": 3,
    "profit warning": 5, "guidance cut": 5, "guidance raise": 4,
    "regulatory action": 5, "ban": 5, "raid": 5, "crore": 2, "%": 1,
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

# Broad Google News queries — kept generic so one call covers many stocks,
# rather than 180 separate per-symbol queries (rate-limit friendly).
GOOGLE_NEWS_QUERIES = [
    "NSE listed company acquisition OR merger OR stake",
    "NSE company board meeting outcome",
    "NSE company profit warning OR guidance",
    "SEBI investigation OR rating downgrade NSE",
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
    fo_symbols = get_fo_symbols()
    print(f"Loaded {len(fo_symbols)} F&O symbols")

    raw_items = []
    raw_items += fetch_nse_announcements()
    for q in GOOGLE_NEWS_QUERIES:
        raw_items += fetch_google_news(q)
    raw_items += fetch_static_rss()
    print(f"Fetched {len(raw_items)} raw items")

    seen = load_seen()
    alerts_by_symbol = {}

    for item in raw_items:
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

    save_seen(seen)

    if not alerts_by_symbol:
        print("No material alerts this cycle.")
        return

    lines = [f"*F&O News Digest — {datetime.now().strftime('%d %b %H:%M')}*\n"]
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


if __name__ == "__main__":
    main()
