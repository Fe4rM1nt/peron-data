"""Build a date-specific, conservative railway metadata feed for Peron.

Input is live GTFS and official KM rolling-stock PDFs. A missing or ambiguous
assignment is deliberately omitted. The previous JSON is never overwritten on
failed validation.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
import zipfile
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from html import unescape
from pathlib import Path
import unicodedata
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlparse

import holidays
import pdfplumber
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "peron.json"
BOARD_DIR = ROOT / "data" / "boards"
STATIONS_OUTPUT = ROOT / "data" / "stations.json"
GTFS = "https://mkuran.pl/gtfs/polish_trains.zip"
KM_INDEX = "https://www.mazowieckie.com.pl/pl/rozklad-jazdy"
KM_PREV = "https://www.mazowieckie.com.pl/pl/podstawowe-informacje/rozklad-jazdy-pociagow-km-wazny-w-dniach-25-x-12-xii-2026r"
KM_NEXT = "https://www.mazowieckie.com.pl/pl/podstawowe-informacje/rozklad-jazdy-pociagow-km-wazny-w-dniach-13-xii-2026r-13-iii-2027r"
MONTHS = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7,
          "VIII": 8, "IX": 9, "X": 10, "XI": 11, "XII": 12}
TRAIN_NUMBER = re.compile(r"(?<!\d)(\d{4,6})(?!\d)")
PDF_PERIOD = re.compile(r"okres\s+obowi[aą]zywania\s*:?\s*(20\d\d[-.]\d\d[-.]\d\d|\d\d[.]\d\d[.]20\d\d)\s*[-–]\s*(20\d\d[-.]\d\d[-.]\d\d|\d\d[.]\d\d[.]20\d\d)", re.I)
session = requests.Session()
session.headers.update({"User-Agent": "PeronScheduleImporter/1.0 (personal railway display)"})


def get(url: str) -> bytes:
    response = session.get(url, timeout=80)
    response.raise_for_status()
    return response.content


def csv_rows(z: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    paths = [p for p in z.namelist() if p.lower().endswith("/" + name) or p.lower() == name]
    if not paths:
        return []
    return list(csv.DictReader(io.StringIO(z.read(paths[0]).decode("utf-8-sig"))))


def each_day(start: date, end: date):
    while start <= end:
        yield start
        start += timedelta(days=1)


def train_numbers(raw: str) -> list[str]:
    """The KM notation 19816/7 refers to numbers 19816 and 19817."""
    match = re.search(r"(?<!\d)(\d{4,6})(?:/(\d{1,3}))?(?!\d)", raw or "")
    if not match:
        return []
    first, suffix = match.groups()
    if suffix is None:
        return [first]
    second = first[:len(first) - len(suffix)] + suffix
    return [first, second] if first != second else [first]


def plain_name(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", (value or "").casefold())
                   if not unicodedata.combining(c))


def station_key(value: str) -> str:
    """A conservative comparison between GTFS and UIC catalog station names."""
    return re.sub(r"[^a-z0-9]", "", plain_name(value))


def train_directions_at_halinow(z: zipfile.ZipFile, trips: dict, services: dict) -> dict:
    """Use the NEXT station in GTFS, never the final destination, for direction.

    Halinów lies on an east-west line. The next station's longitude relative to
    Halinów identifies the side of the platform without a destination list.
    """
    stops = csv_rows(z, "stops.txt")
    halinow_ids = set()
    longitudes = {}
    for stop in stops:
        stop_id = stop.get("stop_id", "")
        try:
            longitudes[stop_id] = float(stop["stop_lon"])
        except (KeyError, ValueError, TypeError):
            continue
        if plain_name(stop.get("stop_name", "")).startswith("halinow"):
            halinow_ids.add(stop_id)
    if not halinow_ids:
        raise RuntimeError("Halinów station was not found in the GTFS stops")

    stop_times = csv_rows(z, "stop_times.txt")
    station_seq = {}
    for row in stop_times:
        if row.get("stop_id") not in halinow_ids or row.get("trip_id") not in trips:
            continue
        try:
            seq = int(row["stop_sequence"])
        except (ValueError, KeyError):
            continue
        trip_id = row["trip_id"]
        if trip_id not in station_seq or seq < station_seq[trip_id][0]:
            station_seq[trip_id] = (seq, row["stop_id"])

    next_stop = {}
    for row in stop_times:
        trip_id = row.get("trip_id", "")
        origin = station_seq.get(trip_id)
        if not origin:
            continue
        try:
            seq = int(row["stop_sequence"])
        except (ValueError, KeyError):
            continue
        if seq <= origin[0] or row.get("stop_id") not in longitudes or row.get("stop_id") in halinow_ids:
            continue
        previous = next_stop.get(trip_id)
        if previous is None or seq < previous[0]:
            next_stop[trip_id] = (seq, row["stop_id"])

    candidates = defaultdict(set)
    for trip_id, (_, stop_id) in next_stop.items():
        origin = station_seq[trip_id][1]
        delta = longitudes[stop_id] - longitudes[origin]
        if abs(delta) < 0.005:
            continue
        side = "east" if delta > 0 else "west"
        carrier, numbers, service_id = trips[trip_id]
        for day in services.get(service_id, ()):
            for number in numbers:
                candidates[(day, carrier, number)].add(side)

    result = defaultdict(lambda: defaultdict(dict))
    for (day, carrier, number), sides in candidates.items():
        if len(sides) == 1:
            result[day][carrier][number] = next(iter(sides))
    return {day: dict(by_carrier) for day, by_carrier in result.items()}


def train_paths(z: zipfile.ZipFile, trips: dict, services: dict) -> tuple[dict, dict]:
    """Keep station sequences, not guessed directions from terminal stations.

    The Site can use the next real GTFS stop after any selected station.
    Paths with ambiguous train numbers are excluded.
    """
    stops = csv_rows(z, "stops.txt")
    parents = {row["stop_id"]: row.get("parent_station") or row["stop_id"]
               for row in stops if row.get("stop_id")}
    names = {row["stop_id"]: row.get("stop_name", "") for row in stops
             if row.get("stop_id") and row.get("stop_name")}
    paths_by_trip = defaultdict(list)
    for row in csv_rows(z, "stop_times.txt"):
        trip_id = row.get("trip_id", "")
        if trip_id not in trips or row.get("stop_id", "") not in parents:
            continue
        try:
            sequence = int(row["stop_sequence"])
        except (ValueError, KeyError):
            continue
        paths_by_trip[trip_id].append((sequence, parents[row["stop_id"]]))

    choices = defaultdict(set)
    for trip_id, entries in paths_by_trip.items():
        path = []
        for _, station in sorted(entries):
            if not path or path[-1] != station:
                path.append(station)
        if len(path) < 2:
            continue
        carrier, numbers, service_id = trips[trip_id]
        for day in services.get(service_id, ()):
            for number in numbers:
                choices[(day, carrier, number)].add(tuple(path))

    routes = defaultdict(lambda: defaultdict(dict))
    used_ids = set()
    for (day, carrier, number), alternatives in choices.items():
        if len(alternatives) == 1:
            path = next(iter(alternatives))
            routes[day][carrier][number] = list(path)
            used_ids.update(path)
    stop_names = {station: names[station] for station in used_ids if station in names}
    return {day: dict(carriers) for day, carriers in routes.items()}, stop_names


def build_station_catalog(stop_names: dict[str, str]) -> list[dict]:
    """Every station used by KM/SKM, keyed by the *same GTFS stop ID* as schedules.

    Bilkom and third-party UIC identifiers are not interchangeable. Never
    attach an unverified external ID to a station.
    """
    stations = []
    for stop_id, name in stop_names.items():
        if re.fullmatch(r"[a-zA-Z0-9_-]{1,30}", stop_id) and name:
            stations.append({"id": "gtfs:" + stop_id, "name": name})
    stations.sort(key=lambda s: (station_key(s["name"]), s["id"]))
    if len(stations) < 80 or not any(station_key(s["name"]) == "halinow" for s in stations):
        raise RuntimeError("GTFS contains too few KM/SKM stations; leaving prior catalog")
    return stations


def gtfs_station_boards(z: zipfile.ZipFile, trips: dict, trip_lines: dict, services: dict,
                        stop_names: dict[str, str], today: date, horizon=4) -> dict:
    """Generate compact, per-station GTFS departures for the next few days.

    This is an independently usable schedule when Bilkom times out. Live
    delays are layered on top by the Site and are never fabricated as zeros.
    """
    stops = csv_rows(z, "stops.txt")
    parents = {row["stop_id"]: row.get("parent_station") or row["stop_id"]
               for row in stops if row.get("stop_id")}
    longitudes = {}
    for row in stops:
        try:
            longitudes[row["stop_id"]] = float(row["stop_lon"])
        except (ValueError, KeyError, TypeError):
            pass
    rows_by_trip = defaultdict(list)
    for row in csv_rows(z, "stop_times.txt"):
        trip_id = row.get("trip_id", "")
        if trip_id not in trips or row.get("stop_id") not in parents:
            continue
        try:
            sequence = int(row["stop_sequence"])
        except (ValueError, KeyError):
            continue
        rows_by_trip[trip_id].append((sequence, row))

    boards = defaultdict(lambda: defaultdict(list))
    last_day = (today + timedelta(days=horizon - 1)).isoformat()
    tz = ZoneInfo("Europe/Warsaw")
    for trip_id, rows in rows_by_trip.items():
        trip_carrier, numbers, service_id = trips[trip_id]
        if not numbers:
            continue
        dates = [day for day in services.get(service_id, ()) if today.isoformat() <= day <= last_day]
        if not dates:
            continue
        # One stop on each station: stop_times may list platforms separately.
        route = []
        for seq, row in sorted(rows):
            parent = parents[row["stop_id"]]
            if route and route[-1][2] == parent:
                continue
            route.append((seq, row, parent))
        if len(route) < 2:
            continue
        destination = stop_names.get(route[-1][2], "")
        if not destination:
            continue
        for (seq, row, station_id), (_, _, next_id) in zip(route, route[1:]):
            if station_id not in stop_names or next_id not in stop_names:
                continue
            scheduled = (row.get("departure_time") or "").strip()
            m = re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})", scheduled)
            if not m:
                continue
            seconds = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
            # Only allow plausible GTFS time values, including post-midnight 24+.
            if seconds >= 48 * 3600 or int(m[2]) > 59 or int(m[3]) > 59:
                continue
            direction = None
            if station_key(stop_names[station_id]) == "halinow":
                delta = longitudes.get(next_id, 0) - longitudes.get(station_id, 0)
                if abs(delta) > .005:
                    direction = "east" if delta > 0 else "west"
            for day in dates:
                start = datetime.combine(date.fromisoformat(day), time.min, tzinfo=tz)
                timestamp = int((start + timedelta(seconds=seconds)).timestamp())
                data = {"trainCode": trip_carrier + " " + numbers[0],
                        "timestamp": timestamp, "calculatedTime": timestamp,
                        "arrivalStation": destination, "currentStation": stop_names[station_id],
                        "nextStop": stop_names[next_id], "line": trip_lines[trip_id],
                        "tripId": trip_id, "serviceDay": day,
                        "stopId": row["stop_id"], "stopSequence": seq,
                        "trainNumber": numbers[0], "carrier": trip_carrier,
                        "platform": row.get("platform_code") or "", "track": ""}
                if direction:
                    data["direction"] = direction
                boards[station_id][day].append(data)
    return {station: {day: sorted(departures, key=lambda t: t["timestamp"])
                      for day, departures in days.items()} for station, days in boards.items()}


def build_gtfs(today: date, horizon=43) -> tuple[dict, dict, dict, dict, dict]:
    with zipfile.ZipFile(io.BytesIO(get(GTFS))) as z:
        agencies = {r.get("agency_id", ""): r.get("agency_name", "") for r in csv_rows(z, "agency.txt")}
        routes = {}
        for row in csv_rows(z, "routes.txt"):
            line = (row.get("route_short_name") or "").strip().upper()
            if not re.fullmatch(r"(?:RE?\d+[A-Z]?|S\d{1,2}|SKM\d{1,2})", line):
                continue
            agency = agencies.get(row.get("agency_id", ""), "").lower()
            carrier = "SKM" if ("szybka kolej miejska" in agency or "skm" in agency) and "warszaw" in agency else "KM" if "koleje mazowieckie" in agency else ""
            if carrier:
                routes[row["route_id"]] = (carrier, line)

        exceptions = defaultdict(dict)
        for row in csv_rows(z, "calendar_dates.txt"):
            exceptions[row["service_id"]][row["date"]] = row["exception_type"] == "1"
        calendars = {r["service_id"]: r for r in csv_rows(z, "calendar.txt")}
        dates = list(each_day(today, today + timedelta(days=horizon)))
        services = defaultdict(set)
        days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        for service_id in set(calendars) | set(exceptions):
            cal = calendars.get(service_id)
            for d in dates:
                ds = d.strftime("%Y%m%d")
                if ds in exceptions[service_id]:
                    active = exceptions[service_id][ds]
                else:
                    active = bool(cal and cal["start_date"] <= ds <= cal["end_date"] and cal[days[d.weekday()]] == "1")
                if active:
                    services[service_id].add(d.isoformat())

        candidates = defaultdict(set)
        known_trips = {}
        trip_lines = {}
        for trip in csv_rows(z, "trips.txt"):
            route = routes.get(trip.get("route_id", ""))
            if not route:
                continue
            numbers = train_numbers(trip.get("plk_train_number") or trip.get("trip_short_name") or "")
            if not numbers:
                continue
            carrier, line = route
            known_trips[trip["trip_id"]] = (carrier, numbers, trip.get("service_id", ""))
            trip_lines[trip["trip_id"]] = line
            for day in services.get(trip.get("service_id", ""), ()):
                for number in numbers:
                    candidates[(day, carrier, number)].add(line)

        directions = train_directions_at_halinow(z, known_trips, services)
        paths, stop_names = train_paths(z, known_trips, services)
        boards = gtfs_station_boards(z, known_trips, trip_lines, services, stop_names, today)

    result = defaultdict(lambda: defaultdict(dict))
    for (day, carrier, number), lines in candidates.items():
        if len(lines) == 1:
            result[day][carrier][number] = next(iter(lines))
    return {d: dict(by_carrier) for d, by_carrier in result.items()}, directions, paths, stop_names, boards


def pdf_urls() -> list[str]:
    pages = {KM_PREV, KM_NEXT}
    soup = BeautifulSoup(get(KM_INDEX), "html.parser")
    for a in soup.select("a[href]"):
        label = a.get_text(" ", strip=True).lower()
        href = urljoin(KM_INDEX, a["href"])
        if "rozklad-jazdy-pociagow-km-wazny" in href and "mazowieckie.com.pl" in urlparse(href).netloc:
            pages.add(href)
    found = set()
    for page in sorted(pages):
        try:
            content = BeautifulSoup(get(page), "html.parser")
        except requests.RequestException as exc:
            print(f"Notice: cannot read {page}: {exc}", file=sys.stderr)
            continue
        for a in content.select("a[href]"):
            href = urljoin(page, a["href"])
            if not href.lower().split("?")[0].endswith(".pdf"):
                continue
            near = " ".join([href, a.get_text(" ", strip=True), a.parent.get_text(" ", strip=True)[:200]])
            if "zestawienie" in unescape(near).lower() and "mazowieckie.com.pl" in urlparse(href).netloc:
                found.add(href)
    return sorted(found)


def parse_period(text: str):
    m = PDF_PERIOD.search(text)
    if not m:
        return None
    def parse(s):
        if s[4] in '.-':
            return date.fromisoformat(s.replace('.', '-'))
        return datetime.strptime(s, '%d.%m.%Y').date()
    return parse(m[1]), parse(m[2])


def term_dates(term: str, first: date, last: date, pl_holidays: set[date]) -> set[str]:
    """Select only dates explicitly covered by the published term notation."""
    text = " ".join((term or "").upper().split()).replace("–", "-")
    tags = re.findall(r"\(([^)]+)\)", text)
    if any(not re.fullmatch(r"[BCDE]|[1-7](?:-[1-7])?", tag) for tag in tags):
        return set()
    text = re.sub(r"\([^)]*\)", "", text).strip()
    raw_segments = [s.strip() for s in text.split(',') if s.strip()]
    if not raw_segments:
        return set()
    pattern = re.compile(r"^(\d{1,2})(?:\s+(XII|XI|IX|VIII|VII|VI|IV|III|II|X|V|I))?\s*(?:-\s*(\d{1,2})(?:\s+(XII|XI|IX|VIII|VII|VI|IV|III|II|X|V|I))?)?$" )
    parsed = []
    for segment in raw_segments:
        m = pattern.fullmatch(segment)
        if not m:
            return set()
        parsed.append(list(m.groups()))
    # Months are often written only after the last comma-separated date.
    implied = None
    for segment in reversed(parsed):
        sm, em = segment[1], segment[3]
        if em:
            implied = em
        elif sm:
            implied = sm
        if not sm:
            segment[1] = em or implied
        if segment[2] and not em:
            segment[3] = segment[1]
    def day_in_period(day: int, month: int) -> date | None:
        for year in range(first.year, last.year + 1):
            try:
                value = date(year, month, day)
            except ValueError:
                continue
            if first - timedelta(days=40) <= value <= last + timedelta(days=40):
                return value
        return None
    spans = []
    for start_day, start_month, end_day, end_month in parsed:
        if not start_month:
            return set()
        start = day_in_period(int(start_day), MONTHS[start_month])
        end = day_in_period(int(end_day), MONTHS[end_month]) if end_day and end_month else start
        if not start or not end or start > end:
            return set()
        spans.append((start, end))
    out = set()
    for start, end in spans:
        for d in each_day(max(start, first), min(end, last)):
            weekday, holiday = d.weekday(), d in pl_holidays
            if any(code == "D" and (weekday > 4 or holiday) or
                   code == "B" and weekday == 5 or
                   code == "C" and not (weekday >= 5 or holiday) or
                   code == "E" and (weekday == 6 or holiday) or
                   re.fullmatch(r"[1-7](?:-[1-7])?", code) and not (
                       int(code[0]) <= weekday + 1 <= int(code[-1]))
                   for code in tags):
                continue
            out.add(d.isoformat())
    return out


def stock_name(raw_type: str, count: str) -> str:
    models = [re.sub(r"(?:WKM|(?<=45WE)KM)$", "", x.strip().upper()) for x in raw_type.split(",")]
    counts = [int(n) for n in re.findall(r"\d+", count)]
    if not models or not counts:
        return ""
    if models[0] == "EU47":
        # Bogies + command cab: preserve total carriage count, not locomotive.
        return f"EU47 + {sum(counts[1:])} wagonów piętrowych" if len(counts) > 1 else ""
    names = {"ER75": "ER75 FLIRT", "ER160": "ER160 FLIRT", "EN76": "EN76 ELF",
             "45WE": "45WE IMPULS", "EN57AL": "EN57AL", "EN57AKM": "EN57AKM"}
    name = next((v for k,v in names.items() if models[0].startswith(k)),models[0])
    if name and len(models) == 1:
        return f"{counts[0]} × {name}"
    return ""


def candidate_rows(pdf: pdfplumber.PDF):
    for page in pdf.pages:
        for table in page.extract_tables() or []:
            for row in table:
                if row and len(row) >= 8:
                    yield [" ".join(str(c or "").split()) for c in row[:8]]


def build_stock(today: date, horizon=43) -> tuple[dict[str, dict[str, dict]], list[str]]:
    candidates = defaultdict(set)
    links = pdf_urls()
    if not links:
        raise RuntimeError("KM website supplied no rolling-stock PDFs")
    relevant = []
    pl_holidays = set(holidays.country_holidays("PL", years=[today.year, today.year + 1]))
    for url in links:
        try:
            with pdfplumber.open(io.BytesIO(get(url))) as pdf:
                intro = " ".join((p.extract_text() or "")[:1500] for p in pdf.pages[:2])
                period = parse_period(intro)
                if not period:
                    continue
                first, last = period
                if last < today or first > today + timedelta(days=horizon):
                    continue
                relevant.append(url)
                for row in candidate_rows(pdf):
                    number, start, start_time, end, end_time, kind, count, term = row
                    numbers = train_numbers(number)
                    if not numbers or not re.fullmatch(r"\d{1,2}:\d{2}", start_time):
                        continue
                    stock = stock_name(kind, count)
                    if not stock:
                        continue
                    for day in term_dates(term, first, last, pl_holidays):
                        if today.isoformat() <= day <= (today + timedelta(days=horizon)).isoformat():
                            for train_number in numbers:
                                candidates[(day, train_number)].add((stock, url))
        except Exception as exc:
            print(f"Notice: parsing {url} failed: {exc}", file=sys.stderr)
    if not relevant:
        raise RuntimeError("No KM stock PDF matches current or upcoming schedule")
    result = defaultdict(dict)
    for (day, train), alternatives in candidates.items():
        if len({stock for stock, _ in alternatives}) == 1:
            stock, source = sorted(alternatives)[0]
            result[day][train] = {"stock": stock, "source": source, "confidence": "planned"}
    return dict(result), relevant


def main():
    today = datetime.now(timezone.utc).date()
    lines, directions, paths, stop_names, boards = build_gtfs(today)
    stations = build_station_catalog(stop_names)
    stock, sources = build_stock(today)
    line_count = sum(len(rows) for carriers in lines.values() for rows in carriers.values())
    direction_count = sum(len(rows) for carriers in directions.values() for rows in carriers.values())
    stock_count = sum(len(rows) for rows in stock.values())
    path_count = sum(len(rows) for carriers in paths.values() for rows in carriers.values())
    print(f"GTFS: {line_count} lines, {path_count} routes, {direction_count} Halinów directions. Station catalog: {len(stations)}. KM PDF: {stock_count} stock records from {len(sources)} PDFs")
    if line_count < 100 or direction_count < 50 or stock_count < 50:
        raise RuntimeError("Importer generated too few records; leaving previous JSON untouched")
    hal_ids = [s["id"][5:] for s in stations if station_key(s["name"]) == "halinow"]
    if not any(len(boards.get(sid, {}).get(today.isoformat(), [])) > 10 for sid in hal_ids):
        raise RuntimeError("Importer generated no useful Halinów departures; retaining previous data")
    payload = {"schema": 1, "generatedAt": datetime.now(timezone.utc).isoformat(),
               "validUntil": (today + timedelta(days=43)).isoformat(),
               "lines": lines, "directions": directions, "paths": paths, "stopNames": stop_names,
               "stations": stations, "stock": stock,
               "sources": {"lines": GTFS, "directions": GTFS, "paths": GTFS, "stations": GTFS, "stock": sources}}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(OUTPUT)
    STATIONS_OUTPUT.write_text(json.dumps({"generatedAt": payload["generatedAt"], "stations": stations}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    BOARD_DIR.mkdir(parents=True, exist_ok=True)
    for stop_id, days in boards.items():
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,30}", stop_id):
            continue
        for departures in days.values():
            for train in departures:
                stock_record = stock.get(train["serviceDay"], {}).get(train["trainNumber"])
                if train["carrier"] == "KM" and stock_record:
                    train["stock"] = stock_record["stock"]
        (BOARD_DIR / (stop_id + ".json")).write_text(
            json.dumps({"generatedAt": payload["generatedAt"], "stationId": stop_id,
                        "name": stop_names[stop_id], "days": days}, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8")
    print(f"Saved {len(boards)} station boards and {len(stations)} KM/SKM stations")
    print(f"Saved {OUTPUT} ({OUTPUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
