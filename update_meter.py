#!/usr/bin/env python3
"""Collect public border records and official notices, then publish a static snapshot."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
import fcntl
from functools import lru_cache
from hashlib import sha256
import json
from io import StringIO
import os
from pathlib import Path
import re
import sys
from tempfile import NamedTemporaryFile
from unicodedata import normalize
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen
from html.parser import HTMLParser
import xml.etree.ElementTree as ET

from meter_model import (
    CLOSES, DAY, JST, MODEL_HISTORY, RANKS, VALIDATION_HISTORY, as_time, calendar_windows,
    day_window, event_windows, events_at, holidays_at, provisional_context, series_forecast, simultaneous_hours,
    window_months,
)

ROOT = Path(__file__).resolve().parent
BORDERS = "https://www.oshi-p-meter.com/borders"
OFFICIAL = "https://report.pococha.com"
CALENDAR = OFFICIAL + "/n/n803868883e7d"
EVENT_LIST = "https://yotei.ember-w.com"
HOLIDAY_PAGE = "https://www8.cao.go.jp/chosei/shukujitsu/gaiyou.html"
HOLIDAY_CSV = "https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv"
SERIES = {f"{rank}/{hour}": (rank, hour) for hour in CLOSES for rank in RANKS}
ARTICLE = re.compile(r"/n/n[0-9a-f]{12}")
HORIZON = 45
HISTORY = MODEL_HISTORY + VALIDATION_HISTORY
EMPTY_CELL = {"", "-", "−", "—", "–", "まだなし", "データなし", "未掲載"}


class Page(HTMLParser):
    def __init__(self, raw):
        super().__init__(convert_charrefs=True)
        self.tables, self.text, self.anchors, self.images = [], [], [], []
        self.table = self.row = self.cell = None
        self.anchor = None
        self.figures = 0
        self.skip = 0
        self.feed(raw)
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip += 1
        if self.skip:
            return
        if tag in {"h1", "h2", "h3", "h4", "p", "li", "div", "br", "section", "article", "time"}:
            self.text.append("\n")
        if tag == "table":
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in {"th", "td"} and self.row is not None:
            self.cell = []
        elif tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.anchor = href, []
        elif tag == "figure":
            self.figures += 1
        elif tag in {"img", "source"} and self.figures:
            attributes = dict(attrs)
            for key in ("src", "data-src"):
                if attributes.get(key):
                    self.images.append(attributes[key])
            self.images.extend(part.split()[0] for part in attributes.get("srcset", "").split(",") if part.strip())

    def handle_data(self, data):
        if self.skip:
            return
        self.text.append(data)
        if self.cell is not None:
            self.cell.append(data)
        if self.anchor:
            self.anchor[1].append(data)

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.skip = max(0, self.skip - 1)
        if self.skip:
            return
        if tag in {"h1", "h2", "h3", "h4", "p", "li", "div", "section", "article", "time"}:
            self.text.append("\n")
        if tag in {"th", "td"} and self.cell is not None:
            self.row.append(normalize("NFKC", "".join(self.cell)).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None
        elif tag == "a" and self.anchor:
            href, pieces = self.anchor
            self.anchors.append((href, " ".join("".join(pieces).split())))
            self.anchor = None
        elif tag == "figure":
            self.figures = max(0, self.figures - 1)


def get(url):
    request = Request(url, headers={"User-Agent": "PocochaRankMeter/1.0 (west.yokohama/meter)"})
    with urlopen(request, timeout=15) as response:
        host = urlsplit(response.url).hostname
        if host not in {
            "www.oshi-p-meter.com", "oshi-p-meter.com", "report.pococha.com", "yotei.ember-w.com",
            "www8.cao.go.jp",
        }:
            raise ValueError(f"unexpected source redirect for {url}")
        data = response.read(6_000_001)
        if len(data) > 6_000_000:
            raise ValueError(f"source response too large: {url}")
        charset = response.headers.get_content_charset()
        if host == "www8.cao.go.jp" and charset is None:
            try:
                return data.decode("utf-8-sig"), response.url
            except UnicodeDecodeError:
                charset = "cp932"
        return data.decode(charset or "utf-8"), response.url


def parse_holidays(text, first_year, last_year):
    rows = csv.reader(StringIO(text.lstrip("\ufeff")))
    header = next(rows, [])
    if [field.strip() for field in header] != ["国民の祝日・休日月日", "国民の祝日・休日名称"]:
        raise ValueError("Cabinet Office holiday CSV header is missing")
    dates = {}
    for row in rows:
        if not row:
            continue
        label, name = row
        name = name.strip()
        holiday = date(*map(int, label.split("/")))
        if first_year <= holiday.year <= last_year:
            days = dates.setdefault(str(holiday.year), {})
            if not name or holiday in days:
                raise ValueError(f"malformed holiday row for {holiday}")
            days[holiday] = name
    for year, entries in dates.items():
        if len(entries) < 16 or entries.get(date(int(year), 1, 1)) != "元日":
            raise ValueError(f"incomplete holiday data for {year}")
        national_days = {holiday for holiday, label in entries.items() if label != "休日"}
        for holiday, name in list(entries.items()):
            if name == "休日":
                between_holidays = holiday - DAY in national_days and holiday + DAY in national_days
                entries[holiday] = "祝日に挟まれた休日" if between_holidays else "振替休日"
    return {year: {holiday.isoformat(): name for holiday, name in entries.items()}
            for year, entries in dates.items()}


def update_holidays(database, now):
    try:
        raw, _ = get(HOLIDAY_CSV)
        earliest = (now.date() - HISTORY * DAY).year
        latest = (now.date() + HORIZON * DAY).year
        holidays = parse_holidays(raw, earliest, latest)
        if str(now.year) not in holidays:
            raise ValueError("Cabinet Office holiday data is missing the current year")
        for year, dates in holidays.items():
            put_version(database.setdefault("holidays", {}).setdefault(year, []), now.isoformat(), dates)
    except (OSError, ValueError) as error:
        print(f"Cabinet Office holiday data: {error}", file=sys.stderr)
        return True
    return False


@lru_cache(maxsize=24)
def get_page(url):
    raw, final_url = get(url)
    return Page(raw), final_url


def same_site_links(page, source):
    host = urlsplit(source).hostname
    for href, title in page.anchors:
        url = urljoin(source, href)
        parts = urlsplit(url)
        if parts.scheme == "https" and parts.hostname == host:
            yield parts._replace(fragment="").geturl(), title


def parse_points(cell):
    if cell in EMPTY_CELL:
        return None
    match = re.fullmatch(r"([\d,]+(?:\.\d+)?)\s*([KM])?", cell, re.I)
    if not match:
        raise ValueError(f"unrecognized border value: {cell!r}")
    scale = {"": 1, "K": 1_000, "M": 1_000_000}[(match[2] or "").upper()]
    value = round(float(match[1].replace(",", "")) * scale)
    if not 0 <= value <= 1_000_000_000:
        raise ValueError("out-of-range border value")
    return value


def check_values(points):
    if (len(points) != 3 or any(
        v is not None and (type(v) is not int or not 0 <= v <= 1_000_000_000) for v in points
    )):
        raise ValueError("invalid border record")
    present = [v for v in points if v is not None]
    if present != sorted(present):
        raise ValueError("border values are out of order")


def parse_daily(raw, rank, hour):
    page = Page(raw)
    text = normalize("NFKC", "".join(page.text))
    heading = re.search(rf"(\d{{4}})年\s*(\d{{1,2}})月\s*{rank}\s*・?\s*{hour}時締め", text)
    if not heading:
        raise ValueError("month and series heading missing")
    year, month = map(int, heading.groups())
    for table in page.tables:
        if not table or [cell.replace(" ", "") for cell in table[0]][:4] != ["日付", "±0", "+1", "+2"]:
            continue
        rows = {}
        for row in table[1:]:
            label, *cells = row
            match = re.match(r"^(\d{1,2})/(\d{1,2})\s*(?:\(|$)", label)
            if not match or int(match[1]) != month or len(cells) != 3:
                raise ValueError(f"unexpected day row: {row!r}")
            day = date(year, month, int(match[2])).isoformat()
            if day in rows:
                raise ValueError(f"duplicate border day: {day}")
            rows[day] = [parse_points(cell) for cell in cells]
            check_values(rows[day])
        return f"{year:04d}-{month:02d}", {d: p for d, p in rows.items() if any(v is not None for v in p)}
    raise ValueError("daily borders table missing")


def find_border_page(key):
    rank, hour = SERIES[key]
    direct = f"{BORDERS}/{key}"
    try:
        raw, source = get(direct)
        month, points = parse_daily(raw, rank, hour)
        return month, points, source
    except HTTPError as error:
        if error.code not in (404, 410):
            raise
    except ValueError:
        pass
    index, index_url = get_page(BORDERS)
    ranks = [url for url, label in same_site_links(index, index_url) if label == rank]
    for rank_url in dict.fromkeys(ranks):
        page, source = get_page(rank_url)
        candidates = [url for url, label in same_site_links(page, source)
                      if label == f"{hour}時締め" and url != direct]
        for url in dict.fromkeys([source, *candidates]):
            try:
                raw, actual = get(url)
                month, points = parse_daily(raw, rank, hour)
                return month, points, actual
            except HTTPError as error:
                if error.code not in (404, 410):
                    raise
            except ValueError:
                pass
    raise ValueError(f"{key} border page could not be located or checked")


def fetch_series(key, now, backfill):
    rank, hour = SERIES[key]
    today = now.date()
    month, points, source = find_border_page(key)
    current = today.strftime("%Y-%m")
    previous = (today.replace(day=1) - DAY).strftime("%Y-%m")
    if month not in {current, previous}:
        raise ValueError(f"{key} source month is stale: {month}")
    windows = [(points, source)]
    if (backfill or today.day <= 5) and month != previous:
        url = urlsplit(source)
        parameters = dict(parse_qsl(url.query))
        parameters["month"] = previous
        raw, source = get(url._replace(query=urlencode(parameters)).geturl())
        actual_month, points = parse_daily(raw, rank, hour)
        if actual_month != previous:
            raise ValueError(f"{key} expected {previous}, got {actual_month}")
        windows.append((points, source))
    records = {}
    for points, source in windows:
        for day, values in points.items():
            if day_window(date.fromisoformat(day), hour)[1] <= now:
                records[day] = values, source
    if not records:
        raise ValueError(f"{key} has no finalized rows")
    return key, records


DATE_TEXT = (
    r"(?:(?P<year>20\d{2})\s*(?:年|/))?(?P<month>\d{1,2})\s*(?:月|/)"
    r"(?P<day>\d{1,2})日?(?:\s*\([月火水木金土日祝]+\))?"
    r"(?:\s*(?P<hour>\d{1,2}):(?P<minute>\d\d)(?::(?P<second>\d\d))?)?"
)
PERIOD = re.compile(DATE_TEXT + r"\s*[~〜～–—-]\s*" + DATE_TEXT.replace("?P<", "?P<end_"))
EVENT_TITLE = re.compile(r"イベント|ぽこチャレ|ミッション|大収穫祭|開催決定|ランキング")
OTHER_TITLE = re.compile(r"結果|入賞者|アップデート|確認方法|FAQ|イベントカレンダー|選び方|受賞者")
CALENDAR_TITLE = re.compile(r"イベントカレンダー|アイテムイベントまとめ")
LIST_MONTH = re.compile(r"(20\d\d)年\s*(\d{1,2})月のイベント一覧")
LIST_COUNT = re.compile(r"\d{1,2}月のイベント一覧\s*\(\s*([\d,]+)件\s*\)")
LIST_DATE = r"(\d{1,2})/(\d{1,2})\s*\([月火水木金土日祝]+\)"
LIST_ROW = re.compile(
    r"(?m)^[ \t]*" + LIST_DATE + r"(?:\s*[~〜～]\s*" + LIST_DATE + r")?[ \t]*\n"
)
LIST_META = re.compile(
    r"(?m)^[ \t]*([^\n]+?)・(?:([EDCBAS][1-6])\s*[~〜～]\s*([EDCBAS][1-6]))?"
    r"・\s*(\d{1,2}):(\d\d)\s*[~〜～]\s*(\d{1,2}):(\d\d)[ \t]*$"
)
REFERENCE_RANKS = ("E1", "E2", "E3", *RANKS)
EVENT_CLOSING = re.compile(r"[〖【\[](13|22|24)時締め[〗】\]]")


def period_bound(match, prefix, default_year, final=False):
    year = int(match[prefix + "year"] or default_year)
    day = date(year, int(match[prefix + "month"]), int(match[prefix + "day"]))
    hour = match[prefix + "hour"]
    at = datetime.combine(day, time(), JST)
    if hour is None:
        return at + (DAY if final else timedelta())
    at += timedelta(hours=int(hour), minutes=int(match[prefix + "minute"]),
                    seconds=int(match[prefix + "second"] or 0))
    if final:
        at += timedelta(seconds=1 if match[prefix + "second"] else 60)
    return at


def parse_notice(raw, title, published, url):
    page = Page(raw)
    text = normalize("NFKC", "".join(page.text))
    ranges = []
    for block in re.finditer(r"(?:イベント)?開催期間\s*[:：]?\s*([^\n]+)", text):
        match = PERIOD.match(block[1].strip())
        if match:
            ranges.append(match)
    # Multiple periods require the per-stage and per-event mapping in the reviewed file.
    if len(ranges) != 1:
        return []
    match = ranges[0]
    start = period_bound(match, "", published.year)
    end = period_bound(match, "end_", start.year, True)
    if end <= start and not match["end_year"] and start.month >= 11:
        end = period_bound(match, "end_", start.year + 1, True)
    if not timedelta() < end - start <= 60 * DAY:
        return []
    ranks, ranking = None, None
    if re.search(r"全(?:ての)?ランク|すべてのライバー|全ライバー", text):
        ranks = list(RANKS)
    else:
        eligible = re.search(r"(?:対象ランク|参加ランク|応援ランク)\s*:\s*([^\n]+)", text)
        if eligible:
            ranks = set()
            for group, start_n, last_group, end_n in re.findall(
                r"([DCBAS])([1-6])(?:\s*[-~〜～–]\s*([DCBAS])?([1-6]))?", eligible[1]
            ):
                first, last = group + start_n, (last_group or group) + (end_n or start_n)
                if first in RANKS and last in RANKS:
                    ranks.update(RANKS[RANKS.index(first):RANKS.index(last) + 1])
            ranks = sorted(ranks) or None
    if re.search(r"ランキング(?:はありません|なし|を実施しません)|ランキング無", text):
        ranking = False
    elif re.search(r"ランキングを(?:開催|実施)|ランキング形式|ランキング型|ランキングで競", text):
        ranking = True
    elif "ミッション概要" in text and "ランキング" not in text:
        ranking = False
    return [{
        "id": url.rsplit("/", 1)[-1], "name": title[:160], "ranks": ranks,
        "ranking": ranking, "timed": bool(match["hour"] and match["end_hour"]),
        "start": start.isoformat(), "end": end.isoformat(),
        "published": published.isoformat(), "source": url,
    }]


def parse_reference(text, month, source, now):
    text = normalize("NFKC", text)
    heading = LIST_MONTH.search(text)
    count = LIST_COUNT.search(text)
    timestamp = re.search(r"(20\d\d-\d\d-\d\d)\s+(\d\d:\d\d)\s*時点", text)
    if (not heading or f"{int(heading[1]):04d}-{int(heading[2]):02d}" != month
            or not count or not timestamp):
        raise ValueError("event list date, count or revision is missing")
    as_of = datetime.fromisoformat(timestamp[1] + "T" + timestamp[2]).replace(tzinfo=JST)
    if as_of > now:
        raise ValueError("event list revision is in the future")
    year, listed_month = map(int, month.split("-"))
    month_start = date(year, listed_month, 1)
    next_month = (month_start.replace(day=28) + 4 * DAY).replace(day=1)
    body = text[count.end():]
    starts = list(LIST_ROW.finditer(body))
    rows, parsed_count = {}, 0
    for i, match in enumerate(starts):
        end_at = starts[i + 1].start() if i + 1 < len(starts) else len(body)
        block = body[match.end():end_at]
        details = LIST_META.search(block)
        if not details:
            continue
        name = " ".join(block[:details.start()].split())
        if not name or len(name) > 180:
            continue
        start_month, start_day = map(int, match.groups()[:2])
        start_year = year - int(start_month > listed_month)
        start_date = date(start_year, start_month, start_day)
        end_month, end_day = map(int, match.groups()[2:]) if match[3] else (start_month, start_day)
        end_date = date(start_year + int(end_month < start_month), end_month, end_day)
        start_hour, start_min, end_hour, end_min = map(int, details.groups()[3:])
        start = datetime.combine(start_date, time(start_hour, start_min), JST)
        end = datetime.combine(end_date, time(end_hour, end_min), JST) + timedelta(minutes=1)
        if not timedelta() < end - start <= 120 * DAY:
            raise ValueError(f"invalid event duration: {name!r}")
        parsed_count += 1
        if start.date() >= next_month or end <= datetime.combine(month_start, time(), JST):
            continue
        ranks = None
        if details[2] and details[3]:
            if details[2] not in REFERENCE_RANKS or details[3] not in REFERENCE_RANKS:
                continue
            low, high = map(REFERENCE_RANKS.index, details.groups()[1:3])
            ranks = [rank for rank in REFERENCE_RANKS[low:high + 1] if rank in RANKS]
            if not ranks:
                continue
        if "ボイスライバー限定" in name or re.search(r"\bVoice\b", name, re.I):
            continue
        key = f"{event_name(name)}|{start_date}|{','.join(ranks or [])}"
        event_id = "ref-" + sha256(key.encode()).hexdigest()[:18]
        closing = EVENT_CLOSING.search(name)
        rows[event_id] = {
            "id": event_id, "name": name, "source": source, "reference": True,
            "ranks": ranks, "ranking": None, "timed": True,
            "category": details[1].strip(), "closing": int(closing[1]) if closing else None,
            "start": start.isoformat(), "end": end.isoformat(), "published": as_of.isoformat(),
        }
    expected = int(count[1].replace(",", ""))
    if parsed_count != expected or not rows:
        raise ValueError(f"expected {expected} entries, parsed {parsed_count}")
    return {"url": source, "asOf": as_of.isoformat(), "checked": now.isoformat(), "events": list(rows.values())}


def event_name(name):
    return re.sub(r"[\W_]+", "", normalize("NFKC", name).casefold())


def supplement_events(reference, official):
    rows = {}
    for batch in sorted(reference.values(), key=lambda item: item["asOf"]):
        rows.update({event["id"]: event for event in batch["events"]})
    for event in rows.values():
        name = event_name(event["name"])
        if not any(
            as_time(known["start"]) < as_time(event["end"])
            and as_time(event["start"]) < as_time(known["end"])
            and (event_name(known["name"]) in name
                 or ("pocoland" in name and "20stars" in name
                     and "pocoland" in event_name(known["name"]) and "20stars" in event_name(known["name"])))
            for known in official
        ):
            yield event


def reference_month(month, previous, now, optional=False):
    standard = f"{EVENT_LIST}/events/{month}/"
    tried = set()

    def load(url):
        tried.add(url)
        page, source = get_page(url)
        return parse_reference("".join(page.text), month, source, now)

    for url in dict.fromkeys([previous.get(month, {}).get("url"), standard]):
        if url is None:
            continue
        try:
            return load(url)
        except HTTPError as error:
            if error.code not in (404, 410):
                raise
        except ValueError:
            pass
    # The monthly pages have navigation to adjacent months. Follow those links
    # when a saved URL or the current URL convention stops working.
    nearby = [item["url"] for _, item in sorted(previous.items(), reverse=True)[:2]]
    for anchor in [EVENT_LIST + "/guide/", EVENT_LIST + "/", *nearby]:
        try:
            page, source = get_page(anchor)
        except HTTPError as error:
            if error.code in (404, 410):
                continue
            raise
        for url, title in same_site_links(page, source):
            date_in_url = month in url or month.replace("-", "/") in url
            month_label = normalize("NFKC", title) == f"{int(month[5:])}月"
            if (date_in_url or month_label) and url not in tried:
                try:
                    return load(url)
                except (HTTPError, ValueError):
                    continue
    if not optional:
        raise ValueError(f"reference event list for {month} could not be located or checked")
    return None


def update_reference(previous, now):
    current = now.date().replace(day=1)
    upcoming = (current.replace(day=28) + 4 * DAY).replace(day=1)
    horizon_month = (now.date() + HORIZON * DAY).replace(day=1)
    keep = (now.date() - MODEL_HISTORY * DAY).strftime("%Y-%m")
    batches = {month: item for month, item in previous.items() if month >= keep}
    failed = False
    while current <= max(upcoming, horizon_month):
        month = current.strftime("%Y-%m")
        try:
            batch = reference_month(month, batches, now, optional=current > now.date() and month not in batches)
            if batch and month in batches and batch["asOf"] < batches[month]["asOf"]:
                raise ValueError("reference list revision moved backwards")
            if batch:
                batches[month] = batch
        except (OSError, ValueError) as error:
            print(f"Reference event list: {error}", file=sys.stderr)
            failed = True
        current = (current.replace(day=28) + 4 * DAY).replace(day=1)
    return batches, failed


def article_url(url):
    link = urlsplit(url)
    path = link.path
    if link.hostname in {"note.com", "www.note.com"} and path.startswith("/pococha/"):
        path = path.removeprefix("/pococha")
    elif link.hostname not in (None, "report.pococha.com"):
        return None
    return OFFICIAL + path if ARTICLE.fullmatch(path) else None


def official_events(reviewed, now, previous_calendars):
    notices, article_links, calendars = {}, {}, set()
    reviewed_sources = {event["source"] for event in reviewed}
    try:
        raw, _ = get(OFFICIAL + "/rss")
        items = ET.fromstring(raw).findall("./channel/item")
    except (OSError, ET.ParseError):
        items = []
    for item in items:
        title = item.findtext("title", "")
        source = article_url(item.findtext("link", ""))
        if not source:
            continue
        published = parsedate_to_datetime(item.findtext("pubDate", "")).astimezone(JST)
        if published > now or published < now - 120 * DAY:
            continue
        if CALENDAR_TITLE.search(title):
            calendars.add(source)
        elif EVENT_TITLE.search(title) and not OTHER_TITLE.search(title):
            article_links[source] = title, published
    if not items or not calendars:
        try:
            listing, source = get_page(OFFICIAL + "/all")
        except OSError:
            if not items:
                raise
        else:
            links = [(article_url(url), title) for url, title in same_site_links(listing, source)]
            if not items and not any(url for url, _ in links):
                raise ValueError("official RSS and article index are unavailable")
            for url, title in links:
                if not url:
                    continue
                if CALENDAR_TITLE.search(title):
                    calendars.add(url)
                elif EVENT_TITLE.search(title) and not OTHER_TITLE.search(title):
                    article_links.setdefault(url, None)
    if not calendars:
        calendars.update(previous_calendars or [CALENDAR])
    images = {}
    for calendar in sorted(calendars):
        page, source = get_page(calendar)
        images[source] = sorted({
            urlsplit(urljoin(source, href)).path for href in page.images
            if urlsplit(urljoin(source, href)).hostname == "assets.st-note.com"
        })
        for url, _ in same_site_links(page, source):
            article = article_url(url)
            if article and article not in calendars:
                article_links.setdefault(article, None)
    with ThreadPoolExecutor(max_workers=4) as pool:
        candidates = sorted(set(article_links) - reviewed_sources)
        pages = pool.map(lambda source: get(source)[0], candidates)
        for source, raw in zip(candidates, pages):
            metadata = article_links[source]
            if metadata is None:
                article_text = normalize("NFKC", "".join(Page(raw).text))
                timestamp = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日\s+(\d{1,2}):(\d{2})", article_text)
                title = re.search(r"<h1\b[^>]*>(.*?)</h1>", raw, re.S | re.I)
                if not timestamp or not title:
                    continue
                published = datetime(*map(int, timestamp.groups()), tzinfo=JST)
                title = "".join(Page(title[1]).text).strip()
                if published > now or OTHER_TITLE.search(title):
                    continue
            else:
                title, published = metadata
            notices[source] = parse_notice(raw, title, published, source)
    return notices, sorted(calendars), images


class StaleSource(ValueError):
    """The source has not published recent borders yet."""


def read_json(path, default=None):
    if path.exists():
        return json.loads(path.read_text())
    return default


def atomic_json(path, value, mode=0o644):
    with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
            file.flush()
            os.fchmod(file.fileno(), mode)
            os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def put_version(versions, stamp, *values):
    if not versions or versions[-1][1:] != list(values):
        versions.append([stamp, *values])


def inputs_from_seed(seed_path):
    seed = read_json(seed_path)
    grouped = {key: {} for key in SERIES}
    for page, record in seed["pages"].items():
        key, _ = page.rsplit("/", 1)
        for day, values in record["data"].items():
            grouped[key][day] = values, record["url"]
    for hour, record in seed["latest"].items():
        for rank, values in record["data"].items():
            grouped[f"{rank}/{hour}"][record["date"]] = values, record["url"]
    return list(grouped.items()), seed["summaries"]


def build_report(database, summaries, now, prior, bootstrap, calendars):
    today = now.date()
    dates = [today + offset * DAY for offset in range(HORIZON + 1)]
    event_dates = [today - offset * DAY for offset in range(30, 0, -1)] + dates
    official = events_at(database, now)
    reference = database.get("reference", {})
    all_events = sorted([*official, *supplement_events(reference, official)],
                        key=lambda e: (e["start"], e["name"]))
    metadata = {event["id"]: event for event in all_events}
    holidays = holidays_at(database, now)
    hourly_events, event_gaps, holiday_windows = {}, {}, {}
    for hour in CLOSES:
        daily, gaps, holidays_by_day = {}, {}, {}
        for day in event_dates:
            iso = day.isoformat()
            windows = list(event_windows(all_events, None, day, hour))
            if windows:
                daily[iso] = [{
                    "id": event["id"],
                    "hours": round((hi - lo).total_seconds() / 3600, 2),
                    "final": final > 0,
                } for event, lo, hi, final in windows]
            calendar = list(calendar_windows(day, hour, holidays))
            if calendar:
                holidays_by_day[iso] = [{
                    "kind": kind, "name": name,
                    "start": lo.isoformat(), "end": hi.isoformat(),
                    "hours": (hi - lo).total_seconds() / 3600,
                } for kind, name, lo, hi in calendar]
            if day >= today:
                missing = [month for month in window_months(day, hour) if month not in reference]
                if missing:
                    gaps[iso] = missing
        hourly_events[str(hour)] = daily
        event_gaps[str(hour)] = gaps
        holiday_windows[str(hour)] = holidays_by_day
    records_by_series = {
        key: {date.fromisoformat(day): values for day, values in rows.items()}
        for key, rows in database["records"].items()
    }
    context, context_training = provisional_context(
        records_by_series, now, dates, all_events, set(reference), holidays, event_gaps
    )
    result = {}
    for key, (rank, hour) in SERIES.items():
        raw_records = database["records"].get(key, {})
        records = records_by_series.get(key, {})
        peers = [records_by_series[peer] for peer, (other, close) in SERIES.items()
                 if peer != key and close == hour and other[0] == rank[0] and peer in records_by_series]
        forecasts, validation = series_forecast(
            records, peers, rank, hour, database, now, dates, event_gaps[str(hour)], summaries.get(key),
            context[hour].get(rank),
        )
        default = forecasts[today.isoformat()]
        weekday_defaults = {}
        for initial in dates[:7]:
            weekday = initial.strftime("%w")
            typical = forecasts[initial.isoformat()]
            if typical != default and all(
                forecasts[day.isoformat()] == typical for day in dates if day.weekday() == initial.weekday()
            ):
                weekday_defaults[weekday] = typical
        forecasts = {day.isoformat(): forecasts[day.isoformat()] for day in dates
                     if forecasts[day.isoformat()] != weekday_defaults.get(day.strftime("%w"), default)}
        previous = prior.get("series", {}).get(key, {})
        for day in event_dates[:30]:
            iso = day.isoformat()
            value = previous.get("forecasts", {}).get(iso)
            if value is None and prior and prior["today"] <= iso <= prior["last"]:
                value = previous.get("forecastByWeekday", {}).get(day.strftime("%w"), previous.get("forecastDefault"))
            if value is not None:
                forecasts[iso] = value
        overlaps = {}
        for day in event_dates:
            hours = round(simultaneous_hours(list(event_windows(official, rank, day, hour))), 2)
            if hours:
                overlaps[day.isoformat()] = hours
        actuals = {day: [versions[-1][1], versions[-1][2]]
                   for day, versions in raw_records.items() if date.fromisoformat(day) >= today - 30 * DAY}
        latest = max(raw_records, default=None)
        result[key] = {
            "latest": latest, "latestSource": raw_records[latest][-1][2] if latest else None,
            "actuals": actuals, "forecastDefault": default, "forecastByWeekday": weekday_defaults,
            "forecasts": forecasts,
            "validation": validation,
            "overlaps": overlaps,
        }
    newest = max((series["latest"] for series in result.values() if series["latest"]), default=None)
    if not newest or any(not any(point is not None for point in series["forecastDefault"]["points"])
                         for series in result.values()):
        raise ValueError("one or more series lack recent observations")
    return {
        "schema": 1, "updated": now.isoformat(), "observationsUpdated": now.isoformat(),
        "bootstrap": bootstrap, "latest": newest,
        "first": (today - 30 * DAY).isoformat(), "last": dates[-1].isoformat(),
        "today": today.isoformat(), "series": result, "events": metadata,
        "hourlyEvents": hourly_events, "calendars": calendars, "eventGaps": event_gaps,
        "holidayWindows": holiday_windows, "holidaySource": HOLIDAY_PAGE,
        "contextTraining": context_training,
        "referenceMonths": {month: {key: batch[key] for key in ("url", "asOf", "checked")}
                            for month, batch in reference.items()},
        "imageReviewSince": database.get("image_review"),
    }


def run(now, seed_path):
    get_page.cache_clear()
    stamp = now.isoformat()
    state_file = ROOT / ".meter-state.json"
    report_file = ROOT / "data.json"
    prior = read_json(report_file, {})
    database = read_json(state_file, {
        "records": {}, "events": {}, "event_checks": [], "started": stamp,
    })
    reviewed = read_json(ROOT / "events.json")
    if seed_path:
        if prior:
            raise ValueError("seed mode is for the initial build only")
        observations, summaries = inputs_from_seed(seed_path)
        calendars = [CALENDAR]
        notice_batches = {}
        reference_failed = False
        holiday_failed = False
    else:
        with ThreadPoolExecutor(max_workers=4) as pool:
            observations = list(pool.map(
                lambda key: fetch_series(key, now, not database["records"] or prior.get("bootstrap")), SERIES
            ))
        notice_batches, calendars, images = official_events(reviewed, now, prior.get("calendars"))
        if not notice_batches:
            # A valid feed can contain no event announcements.
            print("No new machine-readable event announcements in the official feed.")
        summaries = {}
        database["event_checks"].append(stamp)
        previous_images = database.get("official_images")
        reviewed_versions = database["events"].get("reviewed", [])
        reviewed_changed = reviewed_versions and reviewed_versions[-1][1] != reviewed
        if reviewed_changed:
            database["image_review"] = None
        elif previous_images and images != previous_images:
            database["image_review"] = stamp
        database["official_images"] = images
        database["reference"], reference_failed = update_reference(database.get("reference", {}), now)
        holiday_failed = update_holidays(database, now)
    for key, records in observations:
        series = database["records"].setdefault(key, {})
        for day, (values, source) in records.items():
            check_values(values)
            if day_window(date.fromisoformat(day), SERIES[key][1])[1] <= now:
                put_version(series.setdefault(day, []), stamp, values, source)
        database["records"][key] = {
            day: versions for day, versions in series.items()
            if date.fromisoformat(day) >= now.date() - HISTORY * DAY
        }
    if not seed_path:
        for key, (rank, hour) in SERIES.items():
            series = database["records"][key]
            newest = max(series, default=None)
            recent_count = sum(date.fromisoformat(day) >= now.date() - 30 * DAY for day in series)
            old_latest = prior.get("series", {}).get(key, {}).get("latest")
            if (newest is None or recent_count < 7
                    or (old_latest and newest < old_latest)):
                raise ValueError(f"{rank} {hour}: recent source coverage failed validation")
            if (now.date() - date.fromisoformat(newest)).days > 4:
                raise StaleSource(f"{rank} {hour}: source has no record after {newest}")
    put_version(database["events"].setdefault("reviewed", []), stamp, reviewed)
    for source, events in notice_batches.items():
        versions = database["events"].get(source, [])
        if not events and versions and any(as_time(event["end"]) > now for event in versions[-1][1]):
            raise ValueError(f"an active official event could no longer be parsed: {source}")
        put_version(database["events"].setdefault(source, []), stamp, events)
    for source in {event["source"] for event in reviewed}:
        if source in database["events"]:
            put_version(database["events"][source], stamp, [])
    cutoff = now - HISTORY * DAY
    database["event_checks"] = [v for v in database["event_checks"] if as_time(v) >= cutoff]
    report = build_report(database, summaries, now, prior, bool(seed_path), calendars)
    atomic_json(state_file, database, 0o600)
    atomic_json(report_file, report)
    print(f"Published {len(SERIES)} series, latest border {report['latest']}, generated {stamp}")
    return {"referenceDelayed": reference_failed, "holidayDelayed": holiday_failed}


def main():
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("--seed", type=Path, help="Use verified public values for the first build")
    options = arguments.parse_args()
    now = datetime.now(JST)
    with (ROOT / ".meter-update.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        try:
            source_status = run(now, options.seed)
        except Exception as error:
            atomic_json(ROOT / "status.json", {
                "checked": datetime.now(JST).isoformat(),
                "state": "stale" if isinstance(error, StaleSource) else "delayed",
            })
            print(f"Rank meter update failed: {error}", file=sys.stderr)
            return 1
        atomic_json(ROOT / "status.json", {
            "checked": datetime.now(JST).isoformat(),
            "state": "bootstrap" if options.seed else "ok",
            **source_status,
        })
        return 0


if __name__ == "__main__":
    sys.exit(main())
