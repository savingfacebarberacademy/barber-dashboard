# Saving Face barber dashboard (public)

Public monthly metrics for every barber: services, products and retention, with 12-month history.
Served by GitHub Pages from this repo. No logins.

## Files
- `index.html` — the page (no external libraries; fonts from Google Fonts).
- `data/history.csv` — the only data file: `month,barber,location,services,products,retention`.

## Rules for this repo (it is public)
- Never add tiers, pay, scores, promotion progress, passwords or tokens. Tiers live in the private repo.
- Each month (1st–2nd): the job in the private repo pulls the prior month from Square, Taylor approves it, then it is appended here.

## Data notes
- History Nov 2025 – Oct 2026 was consolidated from the old dashboard sheet on 2026-11-04 with the
  Password and StartingTier columns removed and these October fixes applied: Miles Willis services 192,
  Colten Gallery services 151, Josh Kowaleski products 1, M.K. Welch products 0 and retention 52.
- Products through Oct 2026 use the old rule (no plain Retail, refunds not subtracted).
  From Nov 2026 products include Retail and are net of refunds.
- Blank cells are months the old sheet left empty; they are shown as "—".
- Former barbers are removed. A barber with no services for 2 straight months is dropped from the whole file.
