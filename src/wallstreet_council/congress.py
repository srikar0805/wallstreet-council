"""What members of Congress trade, and what copying them would actually have earned.

Source (phase 1): the House Clerk's financial disclosure portal. Each year's index is a free ZIP of XML listing
every filing; Periodic Transaction Reports (FilingType P) are the trades, one PDF each.

  electronic PTRs (DocID starts with 2)  text PDFs, parsed deterministically from the table rows
  paper PTRs (DocID starts with 8 or 9)  scans; read by Gemini from the PDF itself and marked "vision"

The honest number. A member has up to 45 days to disclose a trade, and nobody can copy it before then. So every
purchase is scored from the first close AFTER its filing date (the day the public could act) to today, against
the S&P 500 over the same span. The trade-date return is kept beside it to show what the delay costs.

Legal note: 5 U.S.C. 13107 bars using these reports for commercial purposes other than news media. Keep the
published tracker free and non-commercial.
"""
from __future__ import annotations

import base64
import io
import json
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

from . import llm, store
from .floor import HOME

INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
PTR_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"
LEGISLATORS_URLS = ["https://unitedstates.github.io/congress-legislators/legislators-historical.json",
                    "https://unitedstates.github.io/congress-legislators/legislators-current.json"]
HONORIFICS = re.compile(r"\b(Mrs|Mr|Ms|Miss|Dr|Hon)\.?(?=\s|$)", re.I)
CACHE = HOME / "congress"
UA = {"User-Agent": "wallstreet-council (open research; github.com/srikar0805/wallstreet-council)"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS gov_filings (
  doc_id TEXT PRIMARY KEY, source TEXT, filer TEXT, state_district TEXT, filed_date TEXT, year INTEGER,
  kind TEXT, parsed INTEGER DEFAULT 0, n_trades INTEGER, error TEXT, fetched REAL
);
CREATE TABLE IF NOT EXISTS gov_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id TEXT, source TEXT, filer TEXT, party TEXT, state_district TEXT,
  owner TEXT, asset TEXT, ticker TEXT, asset_type TEXT, tx_type TEXT, tx_date TEXT, notified_date TEXT,
  filed_date TEXT, amount_min REAL, amount_max REAL, late INTEGER, parse_method TEXT
);
CREATE INDEX IF NOT EXISTS gov_trades_ticker ON gov_trades(ticker);
CREATE INDEX IF NOT EXISTS gov_trades_filed ON gov_trades(filed_date);
"""

TX = re.compile(r"(?P<type>S \(partial\)|P|S|E)\s+(?P<tx>\d{2}/\d{2}/\d{4})\s+(?P<nt>\d{2}/\d{2}/\d{4})\s+"
                r"(?P<amt>Spouse/DC Over \$1,000,000|Over \$[\d,]+|\$[\d,]+\s*-\s*\$[\d,]+|\$[\d,]+)")
TICKER = re.compile(r"\(([A-Z][A-Z0-9.\-/]{0,9})\)\s*\[([A-Z]{2})\]\s*$")
TAG = re.compile(r"\[([A-Z]{2})\]\s*$")
MARKER = re.compile(r"^[A-Z]\s{2,}")  # detail lines such as "F      S     : New" (the PDF font drops letters)
HEADER_BITS = ("Filing ID #", "ID Owner Asset", "Gains >", "$200?", "Clerk of the House", "Name:", "Status:",
               "State/District:", "Amount Cap.", "Notification")
OWNERS = {"SP": "spouse", "JT": "joint", "DC": "dependent child"}
TX_NAMES = {"P": "buy", "S": "sell", "S (partial)": "partial sell", "E": "exchange"}


def _conn():
    c = store.conn()
    c.executescript(SCHEMA)
    return c


def _get(url: str, timeout: int = 60) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def clean_first(first: str) -> str:
    """'Marjorie Taylor Mrs' and 'Charles J. "Chuck"' -> one consistent spelling per member."""
    first = re.sub(r'\s*"[^"]*"', "", first)
    return re.sub(r"\s+", " ", HONORIFICS.sub("", first)).strip()


def _amount(s: str) -> tuple[float | None, float | None]:
    nums = [float(x.replace(",", "")) for x in re.findall(r"\$([\d,]+)", s)]
    if not nums:
        return None, None
    if "Over" in s:
        return nums[0], None
    return nums[0], nums[-1]


def _iso(mdy: str) -> str | None:
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):  # paper filings often write two-digit years
        try:
            return datetime.strptime(mdy.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


# ---- index ---------------------------------------------------------------------------------------------------
def house_index(year: int) -> list[dict]:
    """Every Periodic Transaction Report filed in `year`."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{year}FD.zip"
    stale = not path.exists() or (year >= date.today().year and time.time() - path.stat().st_mtime > 6 * 3600)
    if stale:
        path.write_bytes(_get(INDEX_URL.format(year=year)))
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read(f"{year}FD.xml"))
    out = []
    for m in root:
        if m.findtext("FilingType") != "P":
            continue
        first = clean_first(m.findtext("First") or "")
        out.append({"doc_id": m.findtext("DocID"), "year": year, "state_district": m.findtext("StateDst"),
                    "filer": f"{first} {m.findtext('Last') or ''}".strip(), "last": m.findtext("Last") or "",
                    "filed_date": _iso(m.findtext("FilingDate") or "")})
    return out


# ---- parsing -------------------------------------------------------------------------------------------------
def parse_electronic(text: str) -> list[dict]:
    """Rows from an electronic PTR's extracted text. Deterministic; no model involved."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    trades = []
    i = 0
    while i < len(lines):
        # the amount range sometimes wraps ("$1,000,001 -" / "$5,000,000"): look at this line plus the next
        window = lines[i] + (" " + lines[i + 1] if i + 1 < len(lines) and lines[i].rstrip().endswith("-") else "")
        m = TX.search(window)
        if not m:
            i += 1
            continue
        block = [window[:m.start()].strip()] if window[:m.start()].strip() else []
        j = i - 1
        while j >= 0 and len(block) < 4:
            ln = lines[j].strip()
            if (not ln or MARKER.match(lines[j]) or any(h in ln for h in HEADER_BITS) or TX.search(ln)
                    or "@" in ln or ":" in ln or "$" in ln):
                break
            block.insert(0, ln)
            if re.match(r"^(SP|JT|DC)\s", ln):
                break
            j -= 1
        asset = " ".join(block).strip()
        owner = "self"
        om = re.match(r"^(SP|JT|DC)\s+(.*)$", asset)
        if om:
            owner, asset = OWNERS[om.group(1)], om.group(2)
        tk = TICKER.search(asset)
        tag = TAG.search(asset)
        lo, hi = _amount(m.group("amt"))
        trades.append({"owner": owner, "asset": re.sub(r"\s*\([A-Z0-9.\-/]+\)\s*\[[A-Z]{2}\]\s*$|\s*\[[A-Z]{2}\]\s*$",
                                                        "", asset).strip(),
                       "ticker": tk.group(1).replace("/", ".") if tk else None,
                       "asset_type": tk.group(2) if tk else (tag.group(1) if tag else None),
                       "tx_type": TX_NAMES[m.group("type")], "tx_date": _iso(m.group("tx")),
                       "notified_date": _iso(m.group("nt")), "amount_min": lo, "amount_max": hi,
                       "parse_method": "text"})
        i += 2 if window != lines[i] else 1
    return trades


# Rows as arrays, not objects: long paper filings run past the output limit when every field is named.
VISION_PROMPT = ("This is a scanned US House Periodic Transaction Report. List every transaction, in order, as JSON: "
                 '{"t":[[owner, asset, ticker, type, date, notified, amount], ...]} where owner is SP, JT, DC or self; '
                 "ticker is the exchange ticker or empty; type is P, S, S (partial) or E; dates as written (MM/DD/YYYY); "
                 "amount as written. Copy only what is on the page. JSON only, no spaces between items.")


def parse_scanned(pdf: bytes) -> list[dict]:
    """Paper filings are images; Gemini reads the PDF directly. Marked 'vision' so the page can say so."""
    key = llm._secret("GEMINI_API_KEY", "gemini-api-key")
    body = {"contents": [{"role": "user", "parts": [
        {"inline_data": {"mime_type": "application/pdf", "data": base64.b64encode(pdf).decode()}},
        {"text": VISION_PROMPT}]}], "generationConfig": {"maxOutputTokens": 32000, "temperature": 0}}
    last = None
    for model in ("gemini-3.1-flash-lite", "gemini-3.5-flash"):
        try:
            d = llm._post(f"{llm.GEMINI_BASE}/models/{model}:generateContent", body, {"x-goog-api-key": key}, 180)
            text = "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"])
            data = llm.parse_json(text)
            break
        except (llm.LLMError, KeyError, IndexError) as e:
            last, data = e, None
    if not isinstance(data, dict):
        raise llm.LLMError(f"vision parse failed: {last}")
    out = []
    keys = ("owner", "asset", "ticker", "type", "date", "notified", "amount")
    rows = [dict(zip(keys, r)) if isinstance(r, list) else r for r in (data.get("t") or data.get("trades") or [])]
    for t in rows:
        if not isinstance(t, dict):
            continue
        typ = str(t.get("type", "")).strip()
        if typ not in TX_NAMES:
            continue
        lo, hi = _amount(str(t.get("amount", "")))
        tk = re.sub(r"[^A-Z0-9.]", "", str(t.get("ticker") or "").upper()) or None
        out.append({"owner": OWNERS.get(str(t.get("owner", "")).upper(), "self"), "asset": str(t.get("asset", ""))[:200],
                    "ticker": tk, "asset_type": "ST" if tk else None, "tx_type": TX_NAMES[typ],
                    "tx_date": _iso(str(t.get("date", ""))), "notified_date": _iso(str(t.get("notified", ""))),
                    "amount_min": lo, "amount_max": hi, "parse_method": "vision"})
    return out


# ---- parties -------------------------------------------------------------------------------------------------
def parties() -> dict[tuple[str, str], str]:
    """(last name, state) -> party, from the public-domain congress-legislators dataset (current members override
    former ones, so a name reused in a state resolves to whoever serves now). Only terms since 2015 count."""
    CACHE.mkdir(parents=True, exist_ok=True)
    out = {}
    for url in LEGISLATORS_URLS:
        path = CACHE / url.rsplit("/", 1)[1]
        if not path.exists() or time.time() - path.stat().st_mtime > 7 * 86400:
            path.write_bytes(_get(url, timeout=120))
        for p in json.loads(path.read_text()):
            term = (p.get("terms") or [{}])[-1]
            if term.get("end", "") < "2015":
                continue
            out[(p["name"].get("last", "").lower(), term.get("state", ""))] = term.get("party", "")
    return out


def _ascii(s: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def party_for(filer: str, state: str, pmap: dict[tuple[str, str], str]) -> str:
    """Match on state plus a surname contained in the full name: handles 'Wasserman Schultz', 'Van Epps',
    accents, and suffixes such as 'MD, FACS'. The longest matching surname wins."""
    name = _ascii(filer)
    hits = [(len(last), party) for (last, st), party in pmap.items()
            if st == state and last and re.search(rf"\b{re.escape(_ascii(last))}\b", name)]
    return max(hits)[1] if hits else ""


def normalize_existing() -> int:
    """Re-apply name cleaning and party lookup to rows already stored."""
    pmap = parties()
    n = 0
    with store._lock, _conn() as c:
        for doc, filer, sd in c.execute("SELECT doc_id, filer, state_district FROM gov_filings").fetchall():
            parts = filer.split(" ")
            last = parts[-1] if parts else ""
            fixed = (clean_first(" ".join(parts[:-1])) + " " + last).strip()
            party = party_for(fixed, (sd or "")[:2], pmap)
            c.execute("UPDATE gov_filings SET filer=? WHERE doc_id=?", (fixed, doc))
            n += c.execute("UPDATE gov_trades SET filer=?, party=? WHERE doc_id=?", (fixed, party, doc)).rowcount
    return n


# ---- sync ----------------------------------------------------------------------------------------------------
def _process(f: dict, party: str) -> tuple[dict, list[dict], str | None]:
    pdf_path = CACHE / "ptr" / str(f["year"]) / f"{f['doc_id']}.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not pdf_path.exists():
            pdf_path.write_bytes(_get(PTR_URL.format(year=f["year"], doc=f["doc_id"])))
            time.sleep(0.2)  # be polite to the Clerk's server
        pdf = pdf_path.read_bytes()
        if f["doc_id"].startswith("2"):
            from pypdf import PdfReader
            text = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(pdf)).pages)
            trades = parse_electronic(text)
            kind = "electronic"
        else:
            trades = parse_scanned(pdf)
            kind = "scanned"
        for t in trades:
            t.update({"doc_id": f["doc_id"], "source": "house", "filer": f["filer"], "party": party,
                      "state_district": f["state_district"], "filed_date": f["filed_date"]})
            if t["tx_date"] and f["filed_date"]:
                t["late"] = int((date.fromisoformat(f["filed_date"]) - date.fromisoformat(t["tx_date"])).days > 45)
        return {**f, "kind": kind}, trades, None
    except Exception as e:  # noqa: BLE001
        return {**f, "kind": "electronic" if f["doc_id"].startswith("2") else "scanned"}, [], str(e)[:300]


def sync(years: list[int] | None = None, max_scanned: int = 60, progress=print) -> dict:
    """Fetch and parse every House PTR not yet in the database."""
    years = years or list(range(2024, date.today().year + 1))
    with _conn() as c:
        done = {r[0] for r in c.execute("SELECT doc_id FROM gov_filings WHERE parsed=1")}
    todo = [f for y in years for f in house_index(y) if f["doc_id"] not in done]
    scanned = [f for f in todo if not f["doc_id"].startswith("2")][:max_scanned]
    todo = [f for f in todo if f["doc_id"].startswith("2")] + scanned
    progress(f"{len(todo)} new filings to read ({len(scanned)} scanned)")
    try:
        party_of = parties()
    except Exception:  # noqa: BLE001
        party_of = {}
    added = errors = 0
    with ThreadPoolExecutor(6) as ex:
        futs = [ex.submit(_process, f, party_for(f["filer"], (f["state_district"] or "")[:2], party_of))
                for f in todo]
        for k, fut in enumerate(futs, 1):
            f, trades, err = fut.result()
            with store._lock, _conn() as c:
                c.execute("INSERT OR REPLACE INTO gov_filings (doc_id, source, filer, state_district, filed_date, year, "
                          "kind, parsed, n_trades, error, fetched) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (f["doc_id"], "house", f["filer"], f["state_district"], f["filed_date"], f["year"], f["kind"],
                           0 if err else 1, len(trades), err, time.time()))
                c.execute("DELETE FROM gov_trades WHERE doc_id=?", (f["doc_id"],))
                for t in trades:
                    c.execute("INSERT INTO gov_trades (doc_id, source, filer, party, state_district, owner, asset, ticker, "
                              "asset_type, tx_type, tx_date, notified_date, filed_date, amount_min, amount_max, late, "
                              "parse_method) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (t["doc_id"], t["source"], t["filer"], t["party"], t["state_district"], t["owner"],
                               t["asset"], t["ticker"], t["asset_type"], t["tx_type"], t["tx_date"], t["notified_date"],
                               t["filed_date"], t["amount_min"], t["amount_max"], t.get("late", 0), t["parse_method"]))
            added += len(trades)
            errors += bool(err)
            if k % 50 == 0:
                progress(f"  {k}/{len(todo)} filings, {added} trades")
    return {"filings": len(todo), "trades": added, "errors": errors}


# ---- the honest scorecard ------------------------------------------------------------------------------------
def _prices(tickers: list[str], start: str):
    import pandas as pd
    import yfinance as yf
    path = CACHE / "prices.pkl"
    if path.exists() and time.time() - path.stat().st_mtime < 6 * 3600:
        cached = pd.read_pickle(path)
        if set(tickers) <= set(cached.columns):
            return cached
    data = yf.download(sorted(set(tickers)), start=start, auto_adjust=True, progress=False, threads=True)["Close"]
    if isinstance(data, pd.Series):
        data = data.to_frame(tickers[0])
    data.index = data.index.tz_localize(None) if data.index.tz is not None else data.index
    data.to_pickle(path)
    return data


def scorecard(since: str = "2024-01-01") -> dict:
    """Copy-on-disclosure returns for every stock purchase, against the S&P 500 over the same span."""
    import pandas as pd
    with _conn() as c:
        buys = [dict(r) for r in c.execute(
            "SELECT * FROM gov_trades WHERE tx_type='buy' AND ticker IS NOT NULL AND (asset_type='ST' OR asset_type IS NULL "
            "OR asset_type='EF') AND filed_date>=? ORDER BY filed_date", (since,))]
    if not buys:
        return {"trades": 0}
    px = _prices([b["ticker"] for b in buys] + ["SPY"], since)
    rows = []
    for b in buys:
        t = b["ticker"]
        if t not in px.columns or not b["filed_date"]:
            continue
        s, spy = px[t].dropna(), px["SPY"].dropna()
        after = s[s.index > pd.Timestamp(b["filed_date"])]  # first close after the public could see it
        if after.empty:
            continue
        entry_day = after.index[0]
        spy_after = spy[spy.index >= entry_day]
        if spy_after.empty:
            continue
        ret = (float(s.iloc[-1]) / float(after.iloc[0]) - 1) * 100
        bret = (float(spy.iloc[-1]) / float(spy_after.iloc[0]) - 1) * 100
        on_trade = s[s.index >= pd.Timestamp(b["tx_date"])] if b["tx_date"] else s.iloc[0:0]
        rows.append({**{k: b[k] for k in ("filer", "party", "state_district", "owner", "ticker", "asset", "tx_date",
                                           "filed_date", "amount_min", "amount_max", "late", "parse_method")},
                     "copy_return_pct": round(ret, 2), "sp500_pct": round(bret, 2), "excess_pct": round(ret - bret, 2),
                     "trade_date_return_pct": round((float(s.iloc[-1]) / float(on_trade.iloc[0]) - 1) * 100, 2)
                     if len(on_trade) else None,
                     "days_held": (s.index[-1] - entry_day).days})
    if not rows:
        return {"trades": 0}
    mature = [r for r in rows if r["days_held"] >= 30]

    def agg(rs):
        if not rs:
            return None
        return {"trades": len(rs), "avg_copy_return_pct": round(sum(r["copy_return_pct"] for r in rs) / len(rs), 2),
                "avg_sp500_pct": round(sum(r["sp500_pct"] for r in rs) / len(rs), 2),
                "avg_excess_pct": round(sum(r["excess_pct"] for r in rs) / len(rs), 2),
                "beat_market_pct": round(100 * sum(r["excess_pct"] > 0 for r in rs) / len(rs), 0),
                "avg_delay_cost_pct": round(sum((r["trade_date_return_pct"] or r["copy_return_pct"]) - r["copy_return_pct"]
                                                for r in rs) / len(rs), 2)}
    by_filer: dict[str, list] = {}
    for r in mature:
        by_filer.setdefault(r["filer"], []).append(r)
    filers = sorted(({"filer": f, "party": rs[0]["party"], "state_district": rs[0]["state_district"], **agg(rs)}
                     for f, rs in by_filer.items() if len(rs) >= 10), key=lambda x: -x["avg_excess_pct"])
    return {"overall": agg(mature), "all_including_recent": agg(rows), "filers": filers,
            "recent": sorted(rows, key=lambda r: r["filed_date"], reverse=True)[:40], "as_of": str(date.today())}


def hot_tickers(days: int = 45) -> list[dict]:
    """Stocks the most different members bought or sold in recently disclosed filings."""
    since = (date.today() - timedelta(days=days)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT ticker, SUM(tx_type='buy') buys, SUM(tx_type LIKE '%sell') sells, COUNT(DISTINCT filer) members, "
            "SUM(CASE WHEN tx_type='buy' THEN COALESCE(amount_min,0) END) buy_min FROM gov_trades "
            "WHERE ticker IS NOT NULL AND filed_date>=? GROUP BY ticker ORDER BY members DESC, buys DESC LIMIT 15",
            (since,)).fetchall()
    return [dict(r) for r in rows]


def brief_block() -> dict:
    """What the council sees: recent political buying and the honest copy record."""
    sc = scorecard()
    return {"note": "US House members' disclosed trades. Disclosure comes up to 45 days after the trade; 'copy' returns "
                    "start the day after disclosure, which is when anyone could act.",
            "hot_tickers_45d": hot_tickers(), "copying_record": sc.get("overall"),
            "recent_buys": [{k: r[k] for k in ("filer", "party", "ticker", "tx_date", "filed_date", "amount_min",
                                               "copy_return_pct", "sp500_pct")} for r in sc.get("recent", [])[:12]]}
