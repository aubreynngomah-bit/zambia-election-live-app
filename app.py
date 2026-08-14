import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template
import requests
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("zambia-election-live")

ECZ_URL = "https://results.elections.org.zm/"
FETCH_MINUTES = int(os.getenv("FETCH_MINUTES", "30"))

app = Flask(__name__)
lock = threading.Lock()

cache = {
    "source": ECZ_URL,
    "fetched_at": None,
    "status": "starting",
    "error": None,
    "summary": {
        "constituencies_reporting": 0,
        "total_constituencies": 226,
        "percentage_received": 0.0,
        "valid_votes": None,
        "registered_voters": 8786300,
        "rejected_ballots": None,
        "turnout": None,
    },
    "candidates": []
}

def clean_int(value):
    if value is None:
        return None
    s = re.sub(r"[^\d]", "", str(value))
    return int(s) if s else None

def clean_pct(value):
    if value is None:
        return None
    m = re.search(r"[-+]?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(m.group()) if m else None

# DNS resolution (socket.getaddrinfo) is a blocking syscall that Python's
# socket/requests timeouts do not bound, so a stalled resolver or firewall
# black-hole can hang the fetch indefinitely no matter what timeout is
# passed to requests. Run each fetch in a watchdog-guarded worker so the
# app can never get stuck reporting "starting" forever; a timed-out worker
# thread is abandoned (Python cannot force-kill a thread) but the app
# itself recovers and reports a clear error.
_fetch_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ecz-fetch")

def fetch_results():
    try:
        _fetch_executor.submit(_fetch_once).result(timeout=40)
    except FutureTimeoutError:
        log.warning("ECZ fetch timed out after 40s (network unreachable from this host?)")
        with lock:
            cache["status"] = "error"
            cache["error"] = "Timed out reaching the ECZ results portal from this server"
            cache["fetched_at"] = datetime.now(timezone.utc).isoformat()
    except Exception as exc:
        log.warning("ECZ fetch failed: %s", exc)
        with lock:
            cache["status"] = "error"
            cache["error"] = str(exc)
            cache["fetched_at"] = datetime.now(timezone.utc).isoformat()

def _fetch_once():
    headers = {
        "User-Agent": "ZambiaElectionLive/1.0 (public results aggregator; contact operator before production use)"
    }
    # (connect, read) timeout tuple, plus a hard wall-clock deadline below:
    # a slow/trickling response can keep resetting requests' per-read
    # timeout indefinitely, so bound total fetch time explicitly too.
    r = requests.get(ECZ_URL, headers=headers, timeout=(10, 15), stream=True)
    r.raise_for_status()
    deadline = time.monotonic() + 25
    chunks = []
    for chunk in r.iter_content(chunk_size=8192):
        if time.monotonic() > deadline:
            raise TimeoutError("ECZ fetch exceeded max total duration")
        chunks.append(chunk)
    html = b"".join(chunks).decode(r.encoding or "utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    _parse_and_cache(soup, text)

def _parse_and_cache(soup, text):
    # Summary: "0/226", "12.5% of results received", etc.
    reporting = 0
    total = 226
    m = re.search(r"(\d+)\s*/\s*(\d+)\s+Constituencies Reporting", text, re.I)
    if m:
        reporting, total = int(m.group(1)), int(m.group(2))

    pct = None
    m = re.search(r"(\d+(?:\.\d+)?)%\s+of results received", text, re.I)
    if m:
        pct = float(m.group(1))
    if pct is None and total:
        pct = round(reporting / total * 100, 2)

    registered = None
    m = re.search(r"of\s+([\d,]+)\s+registered", text, re.I)
    if m:
        registered = clean_int(m.group(1))

    valid_votes = None
    # Avoid treating "Pending" as zero.
    m = re.search(r"Valid Votes Cast\s+([\d,]+)", text, re.I)
    if m:
        valid_votes = clean_int(m.group(1))

    rejected = None
    m = re.search(r"Rejected Ballots\s+([\d,]+)", text, re.I)
    if m:
        rejected = clean_int(m.group(1))

    turnout = None
    m = re.search(r"Voter Turnout\s+([\d.]+)%", text, re.I)
    if m:
        turnout = float(m.group(1))

    candidates = []
    # Prefer the first table containing Candidate / Party / Votes.
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        headers_found = [c.get_text(" ", strip=True).lower() for c in rows[0].find_all(["th", "td"])]
        if "candidate" not in headers_found or "votes" not in headers_found:
            continue

        idx = {h: i for i, h in enumerate(headers_found)}
        for row in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["th", "td"])]
            if not cells:
                continue
            def get(name):
                i = idx.get(name)
                return cells[i] if i is not None and i < len(cells) else ""
            name = get("candidate")
            party = get("party")
            votes = clean_int(get("votes"))
            pct_candidate = clean_pct(get("%"))
            status = get("status")
            if name:
                candidates.append({
                    "name": name,
                    "party": party,
                    "votes": votes if votes is not None else 0,
                    "percentage": pct_candidate,
                    "status": status
                })
        break

    # Fallback: parse candidate names from page text if no table was exposed.
    # This keeps the app honest: no invented figures are created.
    if not candidates:
        # Known 2026 presidential candidate list currently exposed by ECZ.
        known = [
            ("Kelvin F BWALYA", "ZMP"), ("Given M CHANSA", "MEE"),
            ("Xavier F CHUNGU", "LDP"), ("Hakainde S HICHILEMA", "UPND"),
            ("Harry KALABA", "CF"), ("Given KATUTA", "IND"),
            ("Howard KUNDA", "ZAWAPA"), ("Fred M'MEMBE", "SP"),
            ("Brian M MUNDUBILE", "NRPUP"), ("Brian MUSHIMBA", "OPP"),
            ("Ackim A NJOBVU", "DU"), ("Daniel C PULE", "CDP"),
            ("Richwell SIAMUNENE", "NFP"), ("Richard SILUMBE", "LM"),
        ]
        if "14 candidates" in text.lower():
            candidates = [
                {"name": n, "party": p, "votes": 0, "percentage": None, "status": ""}
                for n, p in known
            ]

    payload = {
        "source": ECZ_URL,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
        "error": None,
        "summary": {
            "constituencies_reporting": reporting,
            "total_constituencies": total,
            "percentage_received": pct,
            "valid_votes": valid_votes,
            "registered_voters": registered,
            "rejected_ballots": rejected,
            "turnout": turnout,
        },
        "candidates": candidates
    }

    with lock:
        cache.clear()
        cache.update(payload)

@app.get("/")
def index():
    return render_template("index.html")

@app.get("/api/results")
def api_results():
    with lock:
        return jsonify(cache)

@app.get("/health")
def health():
    with lock:
        return jsonify({"status": cache["status"], "fetched_at": cache["fetched_at"]})

def start_scheduler():
    # Run off the boot thread so a slow first fetch can't delay the
    # WSGI server from binding its port / passing a platform health check.
    threading.Thread(target=fetch_results, daemon=True).start()
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(fetch_results, "interval", minutes=FETCH_MINUTES, id="ecz_fetch", replace_existing=True)
    scheduler.start()

# Runs on import so the fetch loop also starts under a WSGI server
# (e.g. `gunicorn app:app`), not just `python app.py`. The Procfile
# pins gunicorn to a single worker so this in-process scheduler
# doesn't end up running multiple times.
start_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
