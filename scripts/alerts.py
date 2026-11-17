#!/usr/bin/env python3
"""Builds the monthly alert (sent as a GitHub issue, which GitHub emails to the owner).

Reads data/history.csv after the update, compares the month with the month before,
and lists anything that looks unusual plus who moved up or down the services ranking.
Only uses the public dashboard columns.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update_dashboard as ud  # noqa: E402

RETENTION_FLAG_POINTS = 15   # retention moved this many points or more
SHOP_FLAG_PCT = 25           # a whole shop's services moved this much or more
FLAG_MARKS = ("🚨", "⚠️", "🆕", "❓", "⏭️", "⬆️", "⬇️", "🗑️")


def ranks(rows):
    """Services rank (1 = most). Ties share a rank."""
    order = sorted(rows, key=lambda r: -ud.num(r["services"]))
    out, last, rank = {}, None, 0
    for i, r in enumerate(order, 1):
        v = ud.num(r["services"])
        if v != last:
            rank, last = i, v
        out[r["barber"]] = rank
    return out


def flags_from_pr_body(text):
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("- ") and line[2:].startswith(FLAG_MARKS):
            out.append(line[2:])
    return out


def build(month, history, pr_flags, mention="", dashboard_url=""):
    prev_month = ud.add_months(month, -1)
    cur = {r["barber"]: r for r in history if r["month"] == month}
    prev = {r["barber"]: r for r in history if r["month"] == prev_month}
    both = sorted(set(cur) & set(prev))

    checks = list(pr_flags)
    for b in both:
        a, c = prev[b]["retention"], cur[b]["retention"]
        if a != "" and c != "":
            d = int(ud.num(c) - ud.num(a))
            if abs(d) >= RETENTION_FLAG_POINTS:
                checks.append(f"🔁 {b}: retention {int(ud.num(a))}% → {int(ud.num(c))}% ({d:+d} points)")
    prev_shop = {loc: s for loc, _, s, _ in ud.shop_totals(list(prev.values()))}
    for loc, _, s, _ in ud.shop_totals(list(cur.values())):
        p = prev_shop.get(loc)
        if p:
            pct = 100 * (s - p) / p
            if abs(pct) >= SHOP_FLAG_PCT:
                checks.append(f"🏪 {loc}: shop services {p} → {s} ({pct:+.0f}%)")

    # Rank movement among barbers who worked both months (so a skipped shop can't distort it).
    r_now = ranks([cur[b] for b in both])
    r_before = ranks([prev[b] for b in both])
    up = sorted((b for b in both if r_now[b] < r_before[b]), key=lambda b: (r_now[b] - r_before[b], r_now[b]))
    down = sorted((b for b in both if r_now[b] > r_before[b]), key=lambda b: (r_before[b] - r_now[b], r_now[b]))

    L = []
    if mention:
        L.append(f"@{mention}")
        L.append("")
    live = f" and is live at {dashboard_url}" if dashboard_url else " and is live"
    L.append(f"The dashboard was updated for **{ud.month_label(month)}**{live}.")
    L.append("")
    L.append(f"## ⚠️ Things to check ({len(checks)})")
    L.append("")
    L.extend(f"- {c}" for c in checks) if checks else L.append("Nothing unusual this month. ✅")
    L.append("")

    def mover_table(names):
        rows = ["| Barber | Shop | Rank | Services |", "|---|---|---|---|"]
        for b in names:
            rows.append(f"| {b} | {cur[b]['location']} | #{r_before[b]} → #{r_now[b]} | "
                        f"{int(ud.num(prev[b]['services']))} → {int(ud.num(cur[b]['services']))} |")
        return rows

    L.append(f"## 📈 Moved up in the services ranking ({len(up)})")
    L.append("")
    L.extend(mover_table(up) if up else ["Nobody moved up."])
    L.append("")
    L.append(f"## 📉 Moved down ({len(down)})")
    L.append("")
    L.extend(mover_table(down) if down else ["Nobody moved down."])
    L.append("")
    L.append("## 🏆 Top 5 this month")
    L.append("")
    L.append("| # | Barber | Shop | Services | Retention |")
    L.append("|---|---|---|---|---|")
    for i, r in enumerate(sorted(cur.values(), key=lambda r: -ud.num(r["services"]))[:5], 1):
        ret = f"{r['retention']}%" if r["retention"] != "" else "—"
        L.append(f"| {i} | {r['barber']} | {r['location']} | {r['services']} | {ret} |")
    L.append("")
    L.append(f"<sub>Rankings compare barbers who worked both {ud.month_label(prev_month)} and "
             f"{ud.month_label(month)}. Retention is flagged when it moves {RETENTION_FLAG_POINTS}+ points; "
             f"a shop is flagged when its services move {SHOP_FLAG_PCT}%+.</sub>")
    return "\n".join(L) + "\n", len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--month", required=True)
    ap.add_argument("--history", default="data/history.csv")
    ap.add_argument("--pr-body", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--mention", default="")
    ap.add_argument("--dashboard-url", default="")
    a = ap.parse_args(argv)
    ud.parse_month(a.month)
    text = ""
    if a.pr_body and os.path.exists(a.pr_body):
        with open(a.pr_body, encoding="utf-8") as f:
            text = f.read()
    body, n = build(a.month, ud.read_history(a.history), flags_from_pr_body(text), a.mention, a.dashboard_url)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(body)
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"checks={n}\n")
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
