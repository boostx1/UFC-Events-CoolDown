#!/usr/bin/env python3
"""
Auto-sync scraper for upcoming combat sports events across:
  - UFC (https://www.ufc.com/events + ESPN UFC fallback)
  - PFL (https://www.pflmma.com/events)
  - RAF Wrestling / Real American Freestyle (https://www.realamericanfreestyle.com)
  - Bare Knuckle FC / BKFC (https://www.bkfc.com/events)

Zero external dependencies (Python 3.9+ stdlib only).
Produces chronologically sorted data/events.json with official names,
official event posters, venues/locations, and exact UTC start times.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import html
import json
import os
import re
import ssl
import sys
import urllib.request

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_PATH = os.path.join(ROOT_DIR, "data", "events.json")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

SSL_CTX = ssl.create_default_context()

# Common timezone offsets (hours relative to UTC) used by publishers
TZ_OFFSETS = {
    "UTC": 0,
    "GMT": 0,
    "BST": 1,
    "CET": 1,
    "CEST": 2,
    "CAT": 2,
    "EET": 2,
    "AST": 3,   # Arabia Standard Time (Saudi Arabia / MENA events)
    "GST": 4,   # Gulf Standard Time (Abu Dhabi / Dubai)
    "EST": -5,
    "EDT": -4,
    "ET": -5,   # Dynamic DST check applied in parse_us_eastern
    "CST": -6,
    "CDT": -5,
    "CT": -6,
    "MST": -7,
    "MDT": -6,
    "PST": -8,
    "PDT": -7,
    "PT": -8,
    "AEST": 10,
    "AEDT": 11,
}


def fetch_url(url, timeout=18):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
        return resp.read().decode("utf-8", errors="replace")


def clean_text(raw):
    if not raw:
        return ""
    txt = re.sub(r"<[^>]+>", " ", raw)
    txt = html.unescape(txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def is_us_dst(year, month, day):
    """Approximate US DST (2nd Sunday in March to 1st Sunday in November)."""
    if month < 3 or month > 11:
        return False
    if 3 < month < 11:
        return True
    # Calculate 2nd Sunday of March
    if month == 3:
        first_dow = datetime(year, 3, 1).weekday()  # Mon=0 .. Sun=6
        first_sun = 1 + (6 - first_dow) % 7
        second_sun = first_sun + 7
        return day >= second_sun
    # Calculate 1st Sunday of November
    first_dow = datetime(year, 11, 1).weekday()
    first_sun = 1 + (6 - first_dow) % 7
    return day < first_sun


def make_slug(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:80]


def extract_meta(page_html, prop):
    patterns = [
        rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{re.escape(prop)}["\']',
    ]
    for pat in patterns:
        m = re.search(pat, page_html, re.I)
        if m:
            return html.unescape(m.group(1)).strip()
    return ""


def normalize_url(url, base):
    if not url:
        return ""
    url = html.unescape(url).strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return base.rstrip("/") + url
    return url


def to_iso_utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_existing_events():
    if not os.path.exists(DATA_PATH):
        return []
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
            return payload.get("events", [])
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 1. UFC SCRAPER (ufc.com/events + individual event pages + ESPN fallback)
# ---------------------------------------------------------------------------
def fetch_ufc_event_detail(item):
    url = item["url"]
    try:
        page = fetch_url(url, timeout=16)
    except Exception:
        return item

    # Official poster from og:image or hero background
    og_img = extract_meta(page, "og:image") or extract_meta(page, "twitter:image")
    hero_img = ""
    # Prefer official EVENT-ART or TEMP-HERO or hero image from ufc.com/images
    for pat in [
        r'(https?://(?:www\.)?ufc\.com/images/[^\s"\'<>]+?(?:EVENT[-_]ART|TEMP[-_]HERO|POSTER|HERO)[^\s"\'<>]*?\.(?:jpg|jpeg|png|webp)(?:\?[^\s"\'<>]*)?)',
        r'class="c-hero__image[^"]*"[^>]*>.*?<img[^>]+src="([^"]+)"',
        r'<picture[^>]*>.*?<img[^>]+src="(https?://(?:www\.)?ufc\.com/images/styles/background_image[^"]+)"',
    ]:
        m = re.search(pat, page, re.I | re.S)
        if m:
            hero_img = normalize_url(m.group(1), "https://www.ufc.com")
            break

    poster = hero_img or normalize_url(og_img, "https://www.ufc.com")
    # Upgrade background_image_sm to background_image_lg if present for crisp display
    if poster and "background_image_sm" in poster:
        # keep exact URL as returned because Drupal itok tokens are tied to style!
        pass

    if poster and "SILHOUETTE" not in poster:
        item["poster"] = poster

    # Official full title from page <title> or og:title
    og_title = extract_meta(page, "og:title")
    title_m = re.search(r"<title>(.*?)</title>", page, re.S | re.I)
    page_title = clean_text(og_title or (title_m.group(1) if title_m else ""))
    page_title = re.sub(r"\s*\|\s*UFC.*$", "", page_title, flags=re.I).strip()

    # Build clean official name
    # e.g., "UFC 335: Oliveira vs. Lopes" or "UFC Fight Night: Adesanya vs. Magomedov"
    slug = url.rstrip("/").split("/")[-1]
    headline = item.get("mainEvent", "")
    if page_title and page_title.upper().startswith("UFC"):
        if ":" not in page_title and headline and "TBD" not in headline.upper():
            item["name"] = f"{page_title}: {headline}"
        else:
            item["name"] = page_title
    else:
        num_m = re.match(r"ufc-(\d+)$", slug)
        if num_m:
            prefix = f"UFC {num_m.group(1)}"
        else:
            prefix = "UFC Fight Night"
        if headline and "TBD" not in headline.upper():
            item["name"] = f"{prefix}: {headline}"
        else:
            item["name"] = prefix

    # Extract venue and location from hero or JSON-LD
    venue = ""
    location = ""
    loc_block = re.search(r'class="field field--name-venue[^"]*"[^>]*>(.*?)</div>\s*</div>', page, re.S)
    if loc_block:
        parts = [clean_text(p) for p in re.findall(r"<(?:h5|span|p|div)[^>]*>([^<]+)</(?:h5|span|p|div)>", loc_block.group(1))]
        parts = [p for p in parts if p]
        if parts:
            venue = parts[0]
            location = ", ".join(parts[1:]) if len(parts) > 1 else ""

    if not venue or not location:
        # Try c-hero__venue or taxonomy-term--type-venue
        v_m = re.search(r'class="c-hero__headline-suffix[^"]*"[^>]*>(.*?)</div>', page, re.S)
        t_m = re.search(r'<div[^>]*class="[^"]*field--name-taxonomy-term-title[^"]*"[^>]*>(.*?)</div>', page, re.S)
        a_m = re.search(r'<p class="address"[^>]*>(.*?)</p>', page, re.S)
        if t_m:
            venue = clean_text(t_m.group(1))
        if a_m:
            # Join address spans cleanly with commas
            spans = [clean_text(s) for s in re.findall(r"<span[^>]*>(.*?)</span>", a_m.group(1), re.S)]
            spans = [s for s in spans if s]
            location = ", ".join(spans) if spans else clean_text(a_m.group(1))

    if venue:
        item["venue"] = venue
    if location:
        item["location"] = location

    return item


def scrape_ufc(now_utc):
    html_text = fetch_url("https://www.ufc.com/events")
    # Scan all event cards; timestamp check cleanly filters to upcoming & live events
    section = html_text

    # Also grab ESPN UFC data for rich venue/city fallback
    espn_lookup = {}
    try:
        end_str = (now_utc + timedelta(days=240)).strftime("%Y%m%d")
        start_str = (now_utc - timedelta(days=1)).strftime("%Y%m%d")
        espn_raw = fetch_url(
            f"https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard?dates={start_str}-{end_str}",
            timeout=12,
        )
        espn_json = json.loads(espn_raw)
        for ev in espn_json.get("events", []):
            ev_date = ev.get("date", "")[:10]
            comp = (ev.get("competitions") or [{}])[0]
            venue_obj = comp.get("venue") or {}
            addr = venue_obj.get("address") or {}
            city_parts = [addr.get("city"), addr.get("state"), addr.get("country")]
            loc_str = ", ".join([p for p in city_parts if p])
            espn_lookup[ev_date] = {
                "name": ev.get("name", ""),
                "venue": venue_obj.get("fullName", ""),
                "location": loc_str,
                "date": ev.get("date", ""),
            }
    except Exception:
        pass

    # Split on outer c-card-event--result article (contains nested c-card--red-blue articles)
    cards = re.split(r'<article[^>]*class="c-card-event--result"', section)[1:]
    raw_items = []
    seen_urls = set()

    for card in cards:
        # Limit chunk to reasonable card boundary so we never bleed into next card
        card = card[:25000]
        href_m = re.search(r'href="(/event/[^"]+)"', card)
        if not href_m:
            continue
        ev_url = "https://www.ufc.com" + href_m.group(1)
        if ev_url in seen_urls:
            continue
        seen_urls.add(ev_url)

        main_ts_m = re.search(r'data-main-card-timestamp="(\d+)"', card)
        prelim_ts_m = re.search(r'data-prelims-card-timestamp="(\d+)"', card)
        early_ts_m = re.search(r'data-early-card-timestamp="(\d+)"', card)

        ts_str = (main_ts_m and main_ts_m.group(1)) or (prelim_ts_m and prelim_ts_m.group(1))
        if not ts_str:
            continue
        start_dt = datetime.fromtimestamp(int(ts_str), tz=timezone.utc)
        # Skip events that finished more than 6 hours ago
        if start_dt < now_utc - timedelta(hours=6):
            continue

        h3_m = re.search(r'c-card-event--result__headline[^>]*>(.*?)</h3>', card, re.S)
        headline = clean_text(h3_m.group(1)) if h3_m else ""
        if headline:
            headline = re.sub(r"\bvs\b(?!\.)", "vs.", headline, flags=re.I)

        # Venue / address on card if present
        venue_m = re.search(r'<div class="field field--name-taxonomy-term-title[^>]*>(.*?)</div>', card, re.S)
        addr_m = re.search(r'<p class="address"[^>]*>(.*?)</p>', card, re.S)
        venue = clean_text(venue_m.group(1)) if venue_m else ""
        location = ""
        if addr_m:
            spans = [clean_text(s) for s in re.findall(r"<span[^>]*>(.*?)</span>", addr_m.group(1), re.S)]
            spans = [s for s in spans if s]
            location = ", ".join(spans) if spans else clean_text(addr_m.group(1))

        prelim_iso = (
            to_iso_utc(datetime.fromtimestamp(int(prelim_ts_m.group(1)), tz=timezone.utc))
            if prelim_ts_m and prelim_ts_m.group(1)
            else None
        )
        early_iso = (
            to_iso_utc(datetime.fromtimestamp(int(early_ts_m.group(1)), tz=timezone.utc))
            if early_ts_m and early_ts_m.group(1)
            else None
        )

        # Enrich from ESPN by matching date +/- 1 day
        for delta_d in (0, -1, 1):
            d_key = (start_dt + timedelta(days=delta_d)).strftime("%Y-%m-%d")
            if d_key in espn_lookup:
                esp = espn_lookup[d_key]
                if not venue and esp["venue"]:
                    venue = esp["venue"]
                if not location and esp["location"]:
                    location = esp["location"]
                break

        slug = href_m.group(1).rstrip("/").split("/")[-1]
        raw_items.append({
            "id": f"ufc-{slug}",
            "promotion": "UFC",
            "promotionName": "UFC",
            "name": headline or slug.replace("-", " ").title(),
            "mainEvent": headline if headline and "TBD" not in headline.upper() else "Main Event TBA",
            "startTime": to_iso_utc(start_dt),
            "prelimsTime": prelim_iso,
            "earlyPrelimsTime": early_iso,
            "venue": venue or "Venue TBA",
            "location": location or "Location TBA",
            "poster": "",
            "url": ev_url,
            "source": "ufc.com",
        })

    # Fetch each UFC event page in parallel to get official posters & full titles
    results = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(fetch_ufc_event_detail, item) for item in raw_items]
        for fut in as_completed(futs):
            results.append(fut.result())

    # Apply ESPN fallback name/location if still missing
    for item in results:
        start_dt = datetime.fromisoformat(item["startTime"].replace("Z", "+00:00"))
        for delta_d in (0, -1, 1):
            d_key = (start_dt + timedelta(days=delta_d)).strftime("%Y-%m-%d")
            if d_key in espn_lookup:
                esp = espn_lookup[d_key]
                if item["venue"] == "Venue TBA" and esp["venue"]:
                    item["venue"] = esp["venue"]
                if item["location"] == "Location TBA" and esp["location"]:
                    item["location"] = esp["location"]
                if esp["name"] and (item["name"].endswith("TBD") or "vs" not in item["name"] and "vs" in esp["name"]):
                    item["name"] = esp["name"]
                break

    return results


# ---------------------------------------------------------------------------
# 2. PFL SCRAPER (pflmma.com/events + detail pages)
# ---------------------------------------------------------------------------
def parse_pfl_datetime(date_str, time_str, url_slug, now_utc):
    # date_str like "Fri, Dec 11" or "Sat, Dec 19"
    m_date = re.search(r"([A-Za-z]{3,9})\s+(\d{1,2})", date_str)
    if not m_date:
        return None
    month_name, day_s = m_date.group(1), int(m_date.group(2))

    # Determine year from URL slug (e.g. pfl-lyon-2026) or infer relative to now
    y_m = re.search(r"(202[6-9]|203\d)", url_slug)
    if y_m:
        year = int(y_m.group(1))
    else:
        year = now_utc.year
        test_dt = datetime.strptime(f"{month_name[:3]} {day_s} {year}", "%b %d %Y").replace(tzinfo=timezone.utc)
        if test_dt < now_utc - timedelta(days=14):
            year += 1

    base_date = datetime.strptime(f"{month_name[:3]} {day_s} {year}", "%b %d %Y")

    # Parse Main Card time first; if blank, use Early Card time
    # Examples:
    # "5pm AST Early Card |  AST Main Card"
    # "6pm CAT Early Card | 6pm CAT Main Card"
    # "11am ET Early Card | 3pm ET Main Card"
    hour, minute, tz_code = 20, 0, "ET"
    main_m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*([A-Z]{2,4})\s*Main Card", time_str or "", re.I)
    early_m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*([A-Z]{2,4})\s*Early Card", time_str or "", re.I)
    chosen = main_m or early_m
    if chosen:
        h = int(chosen.group(1))
        minute = int(chosen.group(2) or 0)
        ampm = chosen.group(3).lower()
        tz_code = chosen.group(4).upper()
        if ampm == "pm" and h != 12:
            h += 12
        elif ampm == "am" and h == 12:
            h = 0
        hour = h
        # If only early card was listed and main card had no hour, main card starts ~2h later or use card start
        # Actually countdown to event start (early card) or main card: using chosen gives accurate card start!

    offset_h = TZ_OFFSETS.get(tz_code, -5)
    if tz_code == "ET":
        offset_h = -4 if is_us_dst(year, base_date.month, base_date.day) else -5
    elif tz_code == "PT":
        offset_h = -7 if is_us_dst(year, base_date.month, base_date.day) else -8

    tz = timezone(timedelta(hours=offset_h))
    dt_local = datetime(year, base_date.month, base_date.day, hour, minute, tzinfo=tz)
    return dt_local.astimezone(timezone.utc)


def fetch_pfl_detail(item):
    try:
        page = fetch_url(item["url"], timeout=15)
    except Exception:
        return item

    # Look for higher-res official event banner (never overwrite with generic default banner)
    og_img = extract_meta(page, "og:image")
    banner_m = re.search(
        r'(https://pflmma-prod\.s3\.amazonaws\.com/events/(?:banners|background_banners)/[^\s"\'<>]+\.(?:jpg|jpeg|png|webp))',
        page,
        re.I,
    )
    if banner_m:
        item["poster"] = banner_m.group(1)
    elif (
        og_img
        and "default" not in og_img.lower()
        and "event-thumb" not in og_img.lower()
        and "placeholder" not in og_img.lower()
        and not item.get("poster")
    ):
        item["poster"] = og_img

    # Extract main event headline if listed
    me_m = re.search(r'<h[123][^>]*class="[^"]*(?:matchup|main-event|headline)[^"]*"[^>]*>(.*?)</h[123]>', page, re.S | re.I)
    if me_m:
        me_txt = clean_text(me_m.group(1))
        if me_txt and "vs" in me_txt.lower():
            item["mainEvent"] = me_txt

    return item


def scrape_pfl(now_utc):
    html_text = fetch_url("https://www.pflmma.com/events")
    m_up = re.search(r'id="nav-upcoming".*?id="nav-past"', html_text, re.S)
    section = m_up.group(0) if m_up else html_text

    chunks = re.split(r'<div class="event-hub\b', section)[1:]
    raw_items = []

    for chunk in chunks:
        h3 = re.search(r"<h3[^>]*>(.*?)</h3>", chunk, re.S)
        title = clean_text(h3.group(1)) if h3 else ""
        if not title:
            continue

        h6s = [clean_text(x) for x in re.findall(r"<h6[^>]*>(.*?)</h6>", chunk, re.S)]
        p_loc = re.search(r"<p[^>]*>(.*?)</p>", chunk, re.S)
        full_loc = clean_text(p_loc.group(1)) if p_loc else ""
        href_m = re.search(r'href="(https?://(?:www\.)?pflmma\.com/event/[^"]+)"', chunk)
        ev_url = href_m.group(1) if href_m else "https://www.pflmma.com/events"
        img_m = re.search(r'<img[^>]+src="([^"]+)"', chunk)
        poster = img_m.group(1) if img_m else ""

        # PFL uses today's date + empty location + base/event-thumb.jpg for unannounced/TBA placeholder events
        if not full_loc and "base/event-thumb.jpg" in poster and len(h6s) <= 1:
            continue

        date_str = h6s[0] if len(h6s) > 0 else ""
        time_str = h6s[1] if len(h6s) > 1 else ""
        start_dt = parse_pfl_datetime(date_str, time_str, ev_url, now_utc)
        if not start_dt or start_dt < now_utc - timedelta(hours=6):
            continue

        # Split venue and city/country
        parts = [p.strip() for p in full_loc.split(",") if p.strip()]
        venue = parts[0] if parts else "Venue TBA"
        location = ", ".join(parts[1:]) if len(parts) > 1 else full_loc or "Location TBA"

        slug = ev_url.rstrip("/").split("/")[-1]
        raw_items.append({
            "id": f"pfl-{slug}",
            "promotion": "PFL",
            "promotionName": "PFL",
            "name": title,
            "mainEvent": f"{title} Fight Card",
            "startTime": to_iso_utc(start_dt),
            "venue": venue,
            "location": location,
            "poster": poster,
            "url": ev_url,
            "source": "pflmma.com",
        })

    # Enrich from detail pages in parallel
    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(fetch_pfl_detail, item) for item in raw_items]
        for fut in as_completed(futs):
            results.append(fut.result())

    # Enrich PFL Lyon main event from ESPN PFL if available
    try:
        end_str = (now_utc + timedelta(days=240)).strftime("%Y%m%d")
        start_str = (now_utc - timedelta(days=1)).strftime("%Y%m%d")
        espn_pfl = json.loads(fetch_url(
            f"https://site.api.espn.com/apis/site/v2/sports/mma/pfl/scoreboard?dates={start_str}-{end_str}",
            timeout=10,
        ))
        for ev in espn_pfl.get("events", []):
            ev_name = ev.get("name", "")
            ev_date = ev.get("date", "")[:10]
            for item in results:
                if item["startTime"][:10] == ev_date and ":" in ev_name:
                    item["name"] = ev_name
                    item["mainEvent"] = ev_name.split(":", 1)[1].strip()
    except Exception:
        pass

    return results


# ---------------------------------------------------------------------------
# 3. RAF WRESTLING SCRAPER (realamericanfreestyle.com + detail pages)
# ---------------------------------------------------------------------------
def parse_raf_date(date_str):
    date_str = date_str.strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y"):
        try:
            d = datetime.strptime(date_str, fmt)
            # Standard RAF broadcast time is 8:00 PM Eastern
            offset_h = -4 if is_us_dst(d.year, d.month, d.day) else -5
            dt_et = datetime(d.year, d.month, d.day, 20, 0, tzinfo=timezone(timedelta(hours=offset_h)))
            return dt_et.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def fetch_raf_detail(item):
    try:
        page = fetch_url(item["url"], timeout=15)
    except Exception:
        return item

    # Extract venue only from structured metadata or event hero header (never athlete bios)
    og_desc = extract_meta(page, "og:description") or extract_meta(page, "description")
    hero_chunk = page[:15000]
    for candidate_src in (og_desc, clean_text(hero_chunk)):
        if not candidate_src:
            continue
        venue_m = re.search(
            r"\b([A-Z][A-Za-z0-9&'\-\.\s]{1,36}(?:Arena|Coliseum|Center|Centre|Garden|Stadium|Pavilion))\b",
            candidate_src,
        )
        if venue_m:
            cand = venue_m.group(1).strip()
            # Reject athlete gym/club names
            if any(bad in cand.lower() for bad in ("training center", "athletic center", "wc ", "club ", "fitness")):
                continue
            for stop in ["Live at ", "at the ", "at ", "From ", "in ", "Tickets ", "Location "]:
                if stop in cand:
                    cand = cand.split(stop)[-1].strip()
            if 4 <= len(cand) <= 50:
                item["venue"] = cand
                break

    # Look for explicit start time in header/meta (e.g. "8:00 PM ET" or "8 PM EST")
    t_m = re.search(
        r"(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\s*(ET|EST|EDT|CT|CST|CDT|PT|PST|PDT)",
        clean_text(hero_chunk),
        re.I,
    )
    if t_m:
        h = int(t_m.group(1))
        m = int(t_m.group(2) or 0)
        ampm = t_m.group(3).upper()
        tz_code = t_m.group(4).upper()
        if ampm == "PM" and h != 12:
            h += 12
        elif ampm == "AM" and h == 12:
            h = 0
        cur_dt = datetime.fromisoformat(item["startTime"].replace("Z", "+00:00"))
        us_date = (cur_dt - timedelta(hours=5)).date()
        offset_h = TZ_OFFSETS.get(tz_code, -5)
        if tz_code == "ET":
            offset_h = -4 if is_us_dst(us_date.year, us_date.month, us_date.day) else -5
        elif tz_code == "CT":
            offset_h = -5 if is_us_dst(us_date.year, us_date.month, us_date.day) else -6
        elif tz_code == "PT":
            offset_h = -7 if is_us_dst(us_date.year, us_date.month, us_date.day) else -8
        dt_exact = datetime(us_date.year, us_date.month, us_date.day, h, m, tzinfo=timezone(timedelta(hours=offset_h)))
        item["startTime"] = to_iso_utc(dt_exact)

    if not item["poster"]:
        og_img = extract_meta(page, "og:image")
        if og_img:
            item["poster"] = og_img

    return item


def scrape_raf(now_utc):
    html_text = fetch_url("https://www.realamericanfreestyle.com/")
    raw_items = []
    seen = set()

    for m in re.finditer(r'<div class="div-block-99[^"]*">', html_text):
        chunk = html_text[m.start():m.start() + 4500]
        bg_m = re.search(r'background-image:url\((?:&quot;|")([^&"]+)(?:&quot;|")\)', chunk)
        t1_m = re.search(r'class="text-block-21"[^>]*>(.*?)</div>', chunk, re.S)
        t2_m = re.search(r'class="text-block-22"[^>]*>(.*?)</div>', chunk, re.S)
        dt_m = re.search(r'class="event-card_date[^"]*"[^>]*>(.*?)</div>', chunk, re.S)
        loc_m = re.search(r'class="event-card_location[^"]*"[^>]*>(.*?)</div>', chunk, re.S)
        href_m = re.search(r'href="(/events/[^"]+)"', chunk)

        t1 = clean_text(t1_m.group(1)) if t1_m else ""
        t2 = clean_text(t2_m.group(1)) if t2_m else ""
        date_str = clean_text(dt_m.group(1)) if dt_m else ""
        location = clean_text(loc_m.group(1)) if loc_m else ""
        poster = html.unescape(bg_m.group(1)) if bg_m else ""
        ev_url = ("https://www.realamericanfreestyle.com" + href_m.group(1)) if href_m else "https://www.realamericanfreestyle.com"

        start_dt = parse_raf_date(date_str)
        if not start_dt or start_dt < now_utc - timedelta(hours=6):
            continue

        # Format official title cleanly: "RAF15" -> "RAF 15"
        pretty_t1 = re.sub(r"^RAF(\d+)$", r"RAF \1", t1.strip(), flags=re.I)
        if t2 and t2.lower() != t1.lower() and t2.lower() != pretty_t1.lower():
            pretty_t2 = re.sub(r"\bvs\b(?!\.)", "vs.", t2, flags=re.I)
            official_name = f"{pretty_t1}: {pretty_t2}"
            main_ev = pretty_t2
        else:
            official_name = pretty_t1 or t2
            main_ev = "Main Event TBA"

        uid = f"raf-{make_slug(pretty_t1 or official_name)}"
        if uid in seen:
            continue
        seen.add(uid)

        raw_items.append({
            "id": uid,
            "promotion": "RAF",
            "promotionName": "RAF Wrestling",
            "name": official_name,
            "mainEvent": main_ev,
            "startTime": to_iso_utc(start_dt),
            "venue": location or "Venue TBA",
            "location": location or "Location TBA",
            "poster": poster,
            "url": ev_url,
            "source": "realamericanfreestyle.com",
        })

    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(fetch_raf_detail, item) for item in raw_items]
        for fut in as_completed(futs):
            results.append(fut.result())
    return results


# ---------------------------------------------------------------------------
# 4. BARE KNUCKLE FC (BKFC) SCRAPER (bkfc.com/events + detail pages)
# ---------------------------------------------------------------------------
def parse_bkfc_datetime(est_str, fallback_date_str):
    # Example: "December 4, 2026 7:00 PM" (in data-event-date-est)
    s = (est_str or "").strip()
    for fmt in ("%B %d, %Y %I:%M %p", "%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M%p"):
        try:
            d = datetime.strptime(s, fmt)
            offset_h = -4 if is_us_dst(d.year, d.month, d.day) else -5
            dt_et = datetime(d.year, d.month, d.day, d.hour, d.minute, tzinfo=timezone(timedelta(hours=offset_h)))
            return dt_et.astimezone(timezone.utc)
        except ValueError:
            continue

    fb = (fallback_date_str or "").strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            d = datetime.strptime(fb, fmt)
            offset_h = -4 if is_us_dst(d.year, d.month, d.day) else -5
            dt_et = datetime(d.year, d.month, d.day, 20, 0, tzinfo=timezone(timedelta(hours=offset_h)))
            return dt_et.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def smart_title_case(s):
    """Turn ALL-CAPS BKFC titles into clean readable official titles."""
    if not s:
        return s
    acronyms = {"BKFC", "BKFSEA", "UFC", "PFL", "RAF", "UK", "USA", "PA", "FL", "CA", "NY", "NC", "NV", "TX", "OH", "IL", "VII", "VI", "IV", "III", "II", "VIII", "IX", "X"}
    words = s.split()
    out = []
    for w in words:
        clean_w = w.strip(".,:-")
        if clean_w.upper() in acronyms:
            out.append(w.upper())
        elif clean_w.lower() == "vs":
            out.append("vs.")
        elif clean_w.lower() == "knucklemania":
            out.append("KnuckleMania")
        else:
            out.append(w.capitalize())
    return " ".join(out)


def fetch_bkfc_detail(item):
    try:
        page = fetch_url(item["url"], timeout=15)
    except Exception:
        return item

    # Always prefer this specific event's own og:image or hero_background (never "Next Event" widgets!)
    og_img = extract_meta(page, "og:image") or extract_meta(page, "twitter:image")
    hero_m = re.search(
        r'class="hero_background".*?<img[^>]+src="(https://static-media\.gigcasters\.com/images/[^"]+)"',
        page,
        re.S | re.I,
    )
    if og_img and "gigcasters.com" in og_img and "Logo" not in og_img:
        item["poster"] = og_img
    elif hero_m:
        item["poster"] = hero_m.group(1)
    elif og_img and not item.get("poster"):
        item["poster"] = og_img

    return item


def scrape_bkfc(now_utc):
    html_text = fetch_url("https://www.bkfc.com/events")

    # Collect hero poster URLs as fallback map
    hero_posters = re.findall(
        r'https://static-media\.gigcasters\.com/images/[^\s"\'<>]+\.(?:jpg|jpeg|png|webp)',
        html_text,
        re.I,
    )
    hero_posters = [u for u in hero_posters if "Logo" not in u and "AVATAR" not in u]

    raw_items = []
    seen = set()

    for m in re.finditer(r'class="events_card_heading"[^>]*>(.*?)</h3>', html_text, re.S):
        raw_title = clean_text(m.group(1))
        if not raw_title:
            continue
        ctx_before = html_text[max(0, m.start() - 500):m.start()]
        ctx_after = html_text[m.end():min(len(html_text), m.end() + 2500)]

        # Skip past events if marked
        if "is--past" in ctx_before[-250:]:
            continue

        href_m = re.search(r'href="(/events/[^"]+)"', ctx_before) or re.search(r'href="(/events/[^"]+)"', ctx_after)
        slug_path = href_m.group(1) if href_m else ""
        ev_url = ("https://www.bkfc.com" + slug_path) if slug_path else "https://www.bkfc.com/events"

        date_local_m = re.search(r'data-event-date-local[^>]*>(.*?)</p>', ctx_after, re.S)
        date_est_m = re.search(r'data-event-date-est[^>]*>(.*?)</div>', ctx_after, re.S)
        loc_m = re.search(r'class="events_card_location".*?<p>(.*?)</p>', ctx_after, re.S)

        est_str = clean_text(date_est_m.group(1)) if date_est_m else ""
        local_str = clean_text(date_local_m.group(1)) if date_local_m else ""
        start_dt = parse_bkfc_datetime(est_str, local_str)
        if not start_dt or start_dt < now_utc - timedelta(hours=6):
            continue

        raw_loc = clean_text(loc_m.group(1)) if loc_m else ""
        # Fix common typo on bkfc.com ("ACRIURE ARENA" -> "Acrisure Arena")
        raw_loc = re.sub(r"\bACRIURE\b", "ACRISURE", raw_loc, flags=re.I)

        if " - " in raw_loc:
            v_raw, l_raw = raw_loc.split(" - ", 1)
            venue = smart_title_case(v_raw.strip())
            location = smart_title_case(l_raw.strip())
        else:
            venue = smart_title_case(raw_loc) if raw_loc else "Venue TBA"
            location = smart_title_case(raw_loc) if raw_loc else "Location TBA"

        official_name = smart_title_case(raw_title)
        # Extract main event if "X vs. Y" appears at end
        vs_m = re.search(r"([A-Za-z'\-]+\s+vs\.?\s+[A-Za-z'\-]+)$", official_name, re.I)
        main_ev = vs_m.group(1) if vs_m else official_name

        # Best-effort match from hero posters before detail fetch
        matched_poster = ""
        tokens = [w.lower() for w in re.findall(r"[A-Za-z0-9]+", raw_title) if len(w) >= 4 and w.lower() not in ("bkfc", "fight", "night")]
        for hp in hero_posters:
            hp_low = hp.lower()
            if any(tok in hp_low for tok in tokens):
                matched_poster = hp
                break

        slug = slug_path.rstrip("/").split("/")[-1] if slug_path else make_slug(official_name)
        uid = f"bkfc-{slug}"
        if uid in seen:
            continue
        seen.add(uid)

        raw_items.append({
            "id": uid,
            "promotion": "BKFC",
            "promotionName": "Bare Knuckle FC",
            "name": official_name,
            "mainEvent": main_ev,
            "startTime": to_iso_utc(start_dt),
            "venue": venue,
            "location": location,
            "poster": matched_poster,
            "url": ev_url,
            "source": "bkfc.com",
        })

    results = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(fetch_bkfc_detail, item) for item in raw_items]
        for fut in as_completed(futs):
            results.append(fut.result())
    return results


# ---------------------------------------------------------------------------
# ORCHESTRATOR: Resilient merge + strict chronological sort
# ---------------------------------------------------------------------------
def run_sync():
    now_utc = datetime.now(timezone.utc)
    existing = load_existing_events()
    existing_by_promo = {}
    for ev in existing:
        existing_by_promo.setdefault(ev.get("promotion"), []).append(ev)

    scrapers = [
        ("UFC", "https://www.ufc.com/events", scrape_ufc),
        ("PFL", "https://www.pflmma.com/events", scrape_pfl),
        ("RAF", "https://www.realamericanfreestyle.com", scrape_raf),
        ("BKFC", "https://www.bkfc.com/events", scrape_bkfc),
    ]

    all_events = []
    sources_meta = {}

    for promo_code, source_url, fn in scrapers:
        try:
            scraped = fn(now_utc)
            if scraped:
                all_events.extend(scraped)
                sources_meta[promo_code] = {
                    "status": "ok",
                    "count": len(scraped),
                    "url": source_url,
                }
                print(f"[sync] {promo_code}: {len(scraped)} upcoming events synced")
            else:
                # Keep non-expired cached events if scraper returned empty
                kept = [
                    ev for ev in existing_by_promo.get(promo_code, [])
                    if datetime.fromisoformat(ev["startTime"].replace("Z", "+00:00")) >= now_utc - timedelta(hours=6)
                ]
                all_events.extend(kept)
                sources_meta[promo_code] = {
                    "status": "cached" if kept else "empty",
                    "count": len(kept),
                    "url": source_url,
                }
                print(f"[sync] {promo_code}: 0 new, kept {len(kept)} cached events")
        except Exception as exc:
            kept = [
                ev for ev in existing_by_promo.get(promo_code, [])
                if datetime.fromisoformat(ev["startTime"].replace("Z", "+00:00")) >= now_utc - timedelta(hours=6)
            ]
            all_events.extend(kept)
            sources_meta[promo_code] = {
                "status": "error",
                "error": str(exc)[:180],
                "count": len(kept),
                "url": source_url,
            }
            print(f"[sync] {promo_code} ERROR: {exc} (kept {len(kept)} cached)", file=sys.stderr)

    # Deduplicate by id and sort strictly chronologically by startTime
    dedup = {}
    for ev in all_events:
        dedup[ev["id"]] = ev

    sorted_events = sorted(
        dedup.values(),
        key=lambda e: (e["startTime"], e["promotion"], e["name"]),
    )

    # Track newly discovered event IDs vs previous dataset
    old_ids = {ev.get("id") for ev in existing}
    new_ids = [ev["id"] for ev in sorted_events if ev["id"] not in old_ids]

    payload = {
        "lastUpdated": to_iso_utc(now_utc),
        "totalEvents": len(sorted_events),
        "sources": sources_meta,
        "events": sorted_events,
    }

    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"[sync] Saved {len(sorted_events)} chronologically sorted events to {DATA_PATH} (+{len(new_ids)} new)")
    return payload, new_ids


if __name__ == "__main__":
    run_sync()
