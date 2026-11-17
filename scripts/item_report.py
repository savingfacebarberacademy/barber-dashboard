#!/usr/bin/env python3
"""Lists every item sold in one Square account for one month, so we can see which items
should count as services and which as products. Item names and counts only.
Writes markdown to the GitHub job summary (or prints it when run locally)."""

import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update_dashboard as ud  # noqa: E402


def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def n(x):
    return int(round(x))


def report(rows, account, month):
    cat = lambda r: (r.get("ItemSales.category_name") or "").strip() or ud.UNCATEGORIZED
    item = lambda r: (r.get("ItemSales.item_name") or "").strip() or "(no name)"
    kind = lambda r: (r.get("ItemSales.line_item_type") or "").strip() or "-"
    sold = lambda r: ud.num(r.get("ItemSales.items_sold_count"))
    net = lambda r: ud.num(r.get("ItemSales.net_quantity"))

    all_items = defaultdict(lambda: [0.0, 0.0])
    services = defaultdict(float)
    products = defaultdict(lambda: [0.0, 0.0])
    locations = defaultdict(float)
    for r in rows:
        all_items[(cat(r), item(r), kind(r))][0] += sold(r)
        all_items[(cat(r), item(r), kind(r))][1] += net(r)
        locations[(r.get("ItemSales.location_name") or "").strip()] += sold(r)
        c = r.get("ItemSales.category_name")
        if ud.is_service(c, account):
            who = (r.get("ItemSales.team_member_performed_by_name") or "").strip() \
                or (r.get("ItemSales.team_member_attributed_to_name") or "").strip()
            name = ud.clean_name(who)
            if not ud.ignored(name, account):
                services[(name, item(r), kind(r))] += sold(r)
        if ud.is_product(c, account, "old"):
            name = ud.clean_name(r.get("ItemSales.team_member_attributed_to_name"))
            if not ud.ignored(name, account):
                products[(name, cat(r), item(r))][0] += sold(r)
                products[(name, cat(r), item(r))][1] += net(r)

    L = [f"# Item report: {account} account, {ud.month_label(month)}", ""]
    L += ["## Location names", "", table(["Location", "Items sold"],
          sorted(((k or "(blank)", n(v)) for k, v in locations.items()), key=lambda x: -x[1])), ""]
    L += ["## Every item", "", table(["Category", "Item", "Line type", "Sold", "Net"],
          [(c, i, k, n(v[0]), n(v[1])) for (c, i, k), v in sorted(all_items.items(), key=lambda kv: (kv[0][0], -kv[1][0]))]), ""]
    L += ["## Counted as services (per barber)", "", table(["Barber", "Item", "Line type", "Sold"],
          [(b, i, k, n(v)) for (b, i, k), v in sorted(services.items(), key=lambda kv: (kv[0][0], -kv[1]))]), ""]
    L += ["## Counted as products (per barber)", "", table(["Barber", "Category", "Item", "Sold", "Net"],
          [(b, c, i, n(v[0]), n(v[1])) for (b, c, i), v in sorted(products.items(), key=lambda kv: (kv[0][0], -kv[1][0]))]), ""]
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account", choices=["main", "camillus"], default="camillus")
    ap.add_argument("--month", default="")
    a = ap.parse_args(argv)
    month = a.month.strip() or ud.previous_month()
    ud.parse_month(month)
    secret = "SQUARE_MAIN_TOKEN" if a.account == "main" else "SQUARE_CAMILLUS_TOKEN"
    token = os.environ.get(secret, "").strip()
    if not token:
        print(f"ERROR: the {secret} secret is not set.", file=sys.stderr)
        return 2
    start, end = ud.month_bounds(month)
    rows = ud.square_load(token, ud._ordered({
        "measures": ["ItemSales.items_sold_count", "ItemSales.net_quantity"],
        "dimensions": [
            "ItemSales.category_name",
            "ItemSales.item_name",
            "ItemSales.line_item_type",
            "ItemSales.team_member_performed_by_name",
            "ItemSales.team_member_attributed_to_name",
            "ItemSales.location_name",
        ],
        "timeDimensions": [{"dimension": "ItemSales.reporting_day", "dateRange": [start, end]}],
    }))
    md = report(rows, a.account, month)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(md)
    print(md)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ud.SquareError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
