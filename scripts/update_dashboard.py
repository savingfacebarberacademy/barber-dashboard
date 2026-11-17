#!/usr/bin/env python3
"""Monthly update for the Saving Face public barber dashboard.

Pulls one month of data from the Square Reporting API and rewrites
data/history.csv (month,barber,location,services,products,retention).

THIS REPOSITORY IS PUBLIC. Only the six columns above may ever be written.
Tokens are read from environment variables and are never printed or saved.

Environment:
  SQUARE_MAIN_TOKEN      required  main Square account
  SQUARE_CAMILLUS_TOKEN  optional  Camillus Square account (skipped if missing)

Usage:
  python scripts/update_dashboard.py                 # previous month
  python scripts/update_dashboard.py --month 2026-11
  python scripts/update_dashboard.py --month 2026-10 --products-rule old --dry-run
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timezone

# --------------------------------------------------------------------------- rules

SQUARE_URL = "https://connect.squareup.com/reporting/v1/load"
SQUARE_VERSION = os.environ.get("SQUARE_VERSION", "2025-10-16")
PAGE_SIZE = 1000

SERVICE_CATEGORIES = ["service", "Service", "Services", "Services- Additional"]
# "Additional Services" is deliberately NOT included.

PRODUCT_CATEGORIES_NEW = ["Apparel", "Product", "Saving Face Product", "Saving Face Retail", "Retail"]
PRODUCT_CATEGORIES_OLD = ["Apparel", "Product", "Saving Face Product", "Saving Face Retail"]
NEW_PRODUCT_RULE_FROM = "2026-11"  # months before this used the old rule

NAME_MAP = {
    "MK Welch": "M.K. Welch",
    "josh kowaleski": "Josh Kowaleski",
    "Taylor K": "Taylor Klein",
}
IGNORE_ALWAYS = {"Taylor Horsman"}
IGNORE_BY_ACCOUNT = {"main": {"Alex Mott"}}  # Alex Mott is reported from the Camillus account

LOCATION_MAP = {"B'Ville": "Baldwinsville", "B’Ville": "Baldwinsville"}

CHANGE_FLAG_PCT = 40
CSV_COLUMNS = ["month", "barber", "location", "services", "products", "retention"]


def clean_name(raw: str | None) -> str:
    name = (raw or "").strip()
    return NAME_MAP.get(name, name)


def clean_location(raw: str | None) -> str:
    loc = (raw or "").strip()
    return LOCATION_MAP.get(loc, loc)


def ignored(name: str, account: str) -> bool:
    return not name or name in IGNORE_ALWAYS or name in IGNORE_BY_ACCOUNT.get(account, set())


def num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


# --------------------------------------------------------------------------- months

def parse_month(s: str) -> tuple[int, int]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", s or ""):
        raise SystemExit(f"Month must look like YYYY-MM, got {s!r}")
    y, m = s.split("-")
    return int(y), int(m)


def add_months(month: str, n: int) -> str:
    y, m = parse_month(month)
    idx = y * 12 + (m - 1) + n
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def month_bounds(month: str) -> tuple[str, str]:
    y, m = parse_month(month)
    nxt = add_months(month, 1)
    ny, nm = parse_month(nxt)
    last = date(ny, nm, 1).toordinal() - 1
    return f"{y:04d}-{m:02d}-01", date.fromordinal(last).isoformat()


def previous_month(today: date | None = None) -> str:
    if today is None:
        try:
            from zoneinfo import ZoneInfo
            today = datetime.now(ZoneInfo("America/New_York")).date()
        except Exception:
            today = datetime.now(timezone.utc).date()
    return add_months(f"{today.year:04d}-{today.month:02d}", -1)


def month_label(month: str) -> str:
    y, m = parse_month(month)
    return date(y, m, 1).strftime("%B %Y")


# --------------------------------------------------------------------------- Square

class SquareError(RuntimeError):
    pass


def square_load(token: str, query: dict) -> list[dict]:
    """POST /reporting/v1/load with limit/offset paging. Returns all rows."""
    rows: list[dict] = []
    offset = 0
    while True:
        q = dict(query, limit=PAGE_SIZE, offset=offset)
        body = json.dumps({"query": q}).encode()
        req = urllib.request.Request(
            SQUARE_URL,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Square-Version": SQUARE_VERSION,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise SquareError(f"Square Reporting API returned HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise SquareError(f"Could not reach Square: {e.reason}") from None
        page = payload.get("data") or []
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE


def _ordered(query: dict) -> dict:
    # Stable ordering so limit/offset pages never overlap or skip rows.
    query["order"] = [[d, "asc"] for d in query.get("dimensions", [])]
    return query


def services_query(month: str) -> dict:
    start, end = month_bounds(month)
    # reporting_day is already a local date: do NOT pass a timezone.
    return _ordered({
        "measures": ["ItemSales.items_sold_count"],
        "dimensions": [
            "ItemSales.team_member_performed_by_name",
            "ItemSales.team_member_attributed_to_name",
            "ItemSales.location_name",
            "ItemSales.category_name",
        ],
        "timeDimensions": [{"dimension": "ItemSales.reporting_day", "dateRange": [start, end]}],
        "filters": [{"member": "ItemSales.category_name", "operator": "equals", "values": SERVICE_CATEGORIES}],
    })


def products_query(month: str, rule: str) -> dict:
    start, end = month_bounds(month)
    cats = PRODUCT_CATEGORIES_OLD if rule == "old" else PRODUCT_CATEGORIES_NEW
    return _ordered({
        "measures": ["ItemSales.items_sold_count", "ItemSales.net_quantity"],
        "dimensions": [
            "ItemSales.team_member_attributed_to_name",
            "ItemSales.location_name",
            "ItemSales.category_name",
        ],
        "timeDimensions": [{"dimension": "ItemSales.reporting_day", "dateRange": [start, end]}],
        "filters": [{"member": "ItemSales.category_name", "operator": "equals", "values": cats}],
    })


def appointments_query(start: str, end: str) -> dict:
    return _ordered({
        "measures": ["AppointmentsReport.count"],
        "dimensions": [
            "AppointmentsReport.team_member_name",
            "AppointmentsReport.location_name",
            "AppointmentsReport.customer_token",
            "AppointmentsReport.status",
        ],
        "timeDimensions": [{"dimension": "AppointmentsReport.local_start_timestamp", "dateRange": [start, end]}],
    })


# --------------------------------------------------------------------------- calculations

def compute_services(rows: list[dict], account: str):
    """-> {barber: {location: services}}  (items_sold_count, not net of refunds)."""
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        if r.get("ItemSales.category_name") not in SERVICE_CATEGORIES:
            continue
        who = (r.get("ItemSales.team_member_performed_by_name") or "").strip() \
            or (r.get("ItemSales.team_member_attributed_to_name") or "").strip()
        name = clean_name(who)
        if ignored(name, account):
            continue
        out[name][clean_location(r.get("ItemSales.location_name"))] += num(r.get("ItemSales.items_sold_count"))
    return out


def compute_products(rows: list[dict], account: str, rule: str) -> dict[str, float]:
    cats = PRODUCT_CATEGORIES_OLD if rule == "old" else PRODUCT_CATEGORIES_NEW
    measure = "ItemSales.items_sold_count" if rule == "old" else "ItemSales.net_quantity"
    out: dict[str, float] = defaultdict(float)
    for r in rows:
        if r.get("ItemSales.category_name") not in cats:
            continue
        name = clean_name(r.get("ItemSales.team_member_attributed_to_name"))
        if ignored(name, account):
            continue
        out[name] += num(r.get(measure))
    return out


def customer_sets(rows: list[dict], account: str) -> dict[str, set[str]]:
    """Accepted appointments only -> {barber: {customer_token}} (per barber, never per shop)."""
    out: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        if r.get("AppointmentsReport.status") != "accepted":
            continue
        token = (r.get("AppointmentsReport.customer_token") or "").strip()
        if not token:
            continue
        name = clean_name(r.get("AppointmentsReport.team_member_name"))
        if ignored(name, account):
            continue
        out[name].add(token)
    return out


def compute_retention(month_rows: list[dict], prior_rows: list[dict], account: str) -> dict[str, int]:
    current = customer_sets(month_rows, account)
    prior = customer_sets(prior_rows, account)
    out = {}
    for barber, custs in current.items():
        if custs:
            returning = len(custs & prior.get(barber, set()))
            out[barber] = half_up(100 * returning / len(custs))
    return out


def pull_account(token: str, account: str, month: str, rule: str, fetch=square_load) -> list[dict]:
    """Returns rows for history.csv for one Square account."""
    start, end = month_bounds(month)
    prior_start, _ = month_bounds(add_months(month, -12))
    _, prior_end = month_bounds(add_months(month, -1))

    services = compute_services(fetch(token, services_query(month)), account)
    products = compute_products(fetch(token, products_query(month, rule)), account, rule)
    retention = compute_retention(
        fetch(token, appointments_query(start, end)),
        fetch(token, appointments_query(prior_start, prior_end)),  # ~19k rows; processed here
        account,
    )
    return build_rows(month, services, products, retention)


def build_rows(month, services, products, retention) -> list[dict]:
    rows = []
    for barber, by_loc in services.items():
        total = half_up(sum(by_loc.values()))
        if total <= 0:
            continue  # skip barbers with 0 services
        location = max(by_loc.items(), key=lambda kv: (kv[1], kv[0]))[0]
        rows.append({
            "month": month,
            "barber": barber,
            "location": location,
            "services": total,
            "products": max(0, half_up(products.get(barber, 0))),
            "retention": retention.get(barber, ""),
        })
    return rows


# --------------------------------------------------------------------------- history + checks

def read_history(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_history(path: str, rows: list[dict]) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in CSV_COLUMNS})
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(buf.getvalue())


def merge(history: list[dict], new_rows: list[dict], month: str, protect_locations: set[str]):
    """Replace `month`, sort its rows, then drop former barbers. Returns (rows, replaced, removed)."""
    replaced = any(r["month"] == month for r in history)
    # Rows of a skipped account (e.g. Camillus without a token) are kept as they were.
    kept = [r for r in history if r["month"] != month or r["location"] in protect_locations]
    kept_now = [r for r in kept if r["month"] == month]
    kept = [r for r in kept if r["month"] != month]
    block = sorted(list(kept_now) + new_rows, key=lambda r: (r["location"], r["barber"]))
    # Insert the month in chronological position, leaving other rows untouched.
    before = [r for r in kept if r["month"] < month]
    after = [r for r in kept if r["month"] > month]
    rows = before + [{k: str(v) for k, v in r.items()} for r in block] + after

    months = sorted({r["month"] for r in rows})
    recent = set(months[-2:])
    active = {r["barber"] for r in rows if r["month"] in recent and num(r["services"]) > 0}
    last_loc = {}
    for r in rows:
        last_loc[r["barber"]] = r["location"]
    removed = sorted(
        b for b in {r["barber"] for r in rows}
        if b not in active and last_loc.get(b) not in protect_locations
    )
    rows = [r for r in rows if r["barber"] not in removed]
    return rows, replaced, removed


def sanity_report(history_before: list[dict], new_rows: list[dict], month: str):
    prev_month = add_months(month, -1)
    prev = {r["barber"]: r for r in history_before if r["month"] == prev_month}
    cur = {r["barber"]: r for r in new_rows}
    new_barbers = sorted(set(cur) - set(prev))
    missing = sorted(set(prev) - set(cur))
    big_moves = []
    for b in sorted(set(cur) & set(prev)):
        before, after = num(prev[b]["services"]), num(cur[b]["services"])
        if before > 0:
            pct = 100 * (after - before) / before
            if abs(pct) > CHANGE_FLAG_PCT:
                big_moves.append((b, int(before), int(after), pct))
    return prev_month, prev, new_barbers, missing, big_moves


def shop_totals(rows: list[dict]) -> list[tuple[str, int, int, int]]:
    t = defaultdict(lambda: [0, 0, 0])
    for r in rows:
        t[r["location"]][0] += 1
        t[r["location"]][1] += int(num(r["services"]))
        t[r["location"]][2] += int(num(r["products"]))
    return [(loc, *v) for loc, v in sorted(t.items())]


def pr_body(month, rule, new_rows, history_before, replaced, removed, notes, skipped_locations=frozenset()) -> str:
    prev_month, prev, new_barbers, missing, big_moves = sanity_report(history_before, new_rows, month)
    L = []
    L.append(f"Automated dashboard update for **{month_label(month)}** (`{month}`).")
    L.append("")
    L.append("Review the numbers below, then merge this pull request to publish them.")
    L.append("")
    L.append("## Summary")
    L.append("")
    L.append("| Barber | Location | Services | vs last month | Products | Retention |")
    L.append("|---|---|---:|---:|---:|---:|")
    for r in sorted(new_rows, key=lambda r: (r["location"], -int(r["services"]), r["barber"])):
        p = prev.get(r["barber"])
        delta = f"{int(r['services']) - int(num(p['services'])):+d}" if p else "new"
        ret = f"{r['retention']}%" if r["retention"] != "" else "—"
        L.append(f"| {r['barber']} | {r['location']} | {r['services']} | {delta} | {r['products']} | {ret} |")
    L.append("")
    L.append("### Shop totals")
    L.append("")
    L.append(f"| Location | Barbers | Services | Products | Services in {month_label(prev_month)} |")
    L.append("|---|---:|---:|---:|---:|")
    prev_tot = {loc: s for loc, _, s, _ in shop_totals(list(prev.values()))}
    all_rows_total = [0, 0, 0, 0]
    for loc, n, s, p in shop_totals(new_rows):
        ps = prev_tot.get(loc)
        L.append(f"| {loc} | {n} | {s} | {p} | {ps if ps is not None else '—'} |")
        all_rows_total = [all_rows_total[0] + n, all_rows_total[1] + s, all_rows_total[2] + p,
                          all_rows_total[3] + (ps or 0)]
    L.append(f"| **All shops** | {all_rows_total[0]} | {all_rows_total[1]} | {all_rows_total[2]} | {all_rows_total[3]} |")
    L.append("")
    L.append("## Sanity checks")
    L.append("")
    L.append("These are flags to look at, not errors. Nothing was blocked.")
    L.append("")
    flags = []
    if replaced:
        flags.append(f"⚠️ **{month} was already in history.csv.** Its rows were replaced with the numbers above.")
    if new_barbers:
        flags.append(f"🆕 New this month (not in {month_label(prev_month)}): " + ", ".join(new_barbers))
    skipped = sorted(b for b in missing if prev[b]["location"] in skipped_locations)
    missing = [b for b in missing if b not in skipped]
    if missing:
        flags.append(f"❓ In {month_label(prev_month)} but missing this month: " + ", ".join(missing))
    if skipped:
        flags.append("⏭️ Not pulled this month (account skipped): " + ", ".join(skipped))
    for b, before, after, pct in big_moves:
        arrow = "⬆️" if pct > 0 else "⬇️"
        flags.append(f"{arrow} {b}: services {before} → {after} ({pct:+.0f}%, more than {CHANGE_FLAG_PCT}%)")
    if removed:
        flags.append("🗑️ Former barbers removed from history.csv (all months), no services in the 2 most "
                     "recent months: " + ", ".join(removed))
    else:
        flags.append("No former barbers removed.")
    flags.extend(notes)
    if not (replaced or new_barbers or missing or skipped or big_moves):
        flags.insert(0, "✅ No new or missing barbers, no month-over-month swings over "
                        f"{CHANGE_FLAG_PCT}%, and {month} was not already present.")
    L.extend(f"- {f}" for f in flags)
    L.append("")
    L.append("<sub>Products rule: " + ("old (items sold, no plain Retail)" if rule == "old"
             else "net quantity incl. Retail (refunds not credited)") +
             ". Retention: share of the barber's customers this month who also saw the same barber in "
             "the previous 12 months.</sub>")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--month", default="", help="YYYY-MM to compute (default: previous month)")
    ap.add_argument("--history", default="data/history.csv")
    ap.add_argument("--products-rule", choices=["auto", "old", "new"], default="auto",
                    help="old = items_sold_count without plain Retail (applied through Oct 2026)")
    ap.add_argument("--dry-run", action="store_true", help="do not write history.csv")
    ap.add_argument("--pr-body", default="", help="write the PR body (markdown) to this file")
    args = ap.parse_args(argv)

    month = args.month.strip() or previous_month()
    parse_month(month)
    rule = args.products_rule
    if rule == "auto":
        rule = "old" if month < NEW_PRODUCT_RULE_FROM else "new"

    main_token = os.environ.get("SQUARE_MAIN_TOKEN", "").strip()
    cam_token = os.environ.get("SQUARE_CAMILLUS_TOKEN", "").strip()
    if not main_token:
        print("ERROR: the SQUARE_MAIN_TOKEN secret is not set.", file=sys.stderr)
        return 2

    notes = []
    new_rows = pull_account(main_token, "main", month, rule)
    protect = set()
    if cam_token:
        cam_rows = pull_account(cam_token, "camillus", month, rule)
        seen = {r["barber"] for r in new_rows}
        cam_new = [r for r in cam_rows if r["barber"] not in seen]
        if cam_new:
            new_rows += cam_new
        else:
            # Never let an empty or wrong Camillus pull wipe out Camillus rows.
            protect = {"Camillus"}
            if cam_rows:
                why = (f"every barber it returned ({len(cam_rows)}) is already in the main account, so the "
                       "SQUARE_CAMILLUS_TOKEN secret probably holds the main account's token")
            else:
                why = "it returned no services at all (wrong token, a Sandbox token, or no data)"
            notes.append("🚨 **Camillus NOT updated:** the Camillus Square account was queried, but " + why +
                         ". Existing Camillus rows were left exactly as they were. Please check the secret.")
    else:
        protect = {"Camillus"}
        notes.append("⏭️ **Camillus skipped:** the SQUARE_CAMILLUS_TOKEN secret is not set, so Camillus "
                     "barbers have no rows this month (they were not treated as former barbers).")

    return finish(month, rule, new_rows, args.history, args.dry_run, args.pr_body, notes, protect)


def finish(month, rule, new_rows, history_path, dry_run, pr_body_path, notes, protect=frozenset()) -> int:
    history = read_history(history_path)
    rows, replaced, removed = merge(history, new_rows, month, set(protect))
    body = pr_body(month, rule, new_rows, history, replaced, removed, notes, set(protect))
    title = f"Dashboard update — {month_label(month)}"
    if not dry_run:
        write_history(history_path, rows)
    if pr_body_path:
        with open(pr_body_path, "w", encoding="utf-8") as f:
            f.write(body)
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"month={month}\ntitle={title}\n")
    print(title)
    print(f"{len(new_rows)} barbers, products rule: {rule}, dry run: {dry_run}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SquareError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
