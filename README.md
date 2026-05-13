# personal-budget

A local-first personal budget app. Flask web UI backed by plain CSV files. Your data never leaves your machine.

## What it does

- Imports raw bank / credit-card CSVs and normalizes them into a single transaction ledger
- Auto-categorizes using built-in merchant heuristics plus your own keyword rules
- Web dashboard with income / consumption / savings rollups, monthly trends, category breakdown
- **Budget Plan**: `Avg Income − Fixed Bills − Required Savings = Available for variable spending`
- **Goals** with required-monthly-contribution computed from target amount and target date
- **Recurring Bills** with auto-detection from your last 3 months of activity
- **Net Worth** tracking via monthly snapshots
- **Monthly drilldown** views with category breakdown and top merchants
- **Learn-as-you-go rules**: categorize once via the UI, future imports auto-apply the rule

## Setup

```bash
git clone https://github.com/Tyler-Irving/personal-budget.git
cd personal-budget

# Install dependencies in a virtualenv
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Seed your CSV files from the templates (real CSVs are gitignored)
for f in *.example.csv; do cp "$f" "${f/.example/}"; done

# Drop your bank exports here
mkdir -p "Transaction CSVs"
# ...copy your statement CSVs into Transaction CSVs/

# Normalize raw bank files into Transactions.csv
.venv/bin/python normalize.py

# Start the web app
.venv/bin/python app.py
```

Then open **http://localhost:5000** in your browser. `Ctrl+C` in the terminal to stop.

## Supported bank CSV formats

Auto-detected by header row:

| Bank / Card | Header columns |
|---|---|
| FirstView Checking | `Date,Account,Description,Check #,Category,Credit,Debit` |
| Capital One credit card | `Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit` |
| Robinhood credit card | `Date,Time,Cardholder,Amount,Points,Balance,Status,Type,Merchant,Description` |

To add a new format, write a `normalize_*` function in `normalize.py` and register it in the `HANDLERS` dict keyed by the `frozenset` of its header columns.

## How re-imports work

`normalize.py` is **safe to re-run**: it reads your existing `Transactions.csv`, matches each new bank row by `(Date, Description, Amount, Account)`, and preserves your manual categorizations. Your work isn't wiped when you import next month's statements.

Anything you bulk-categorize by pattern in the web UI is saved as a rule in `Rules.csv`, so future imports auto-apply it.

## Web app pages

- `/` — Dashboard: KPI cards, monthly trend chart (clickable), category breakdown, goals, budget plan
- `/transactions` — Filter, search, inline categorize, bulk-categorize by selection or pattern
- `/month/YYYY-MM` — Drilldown for a single month: KPIs, category breakdown, top merchants, all transactions
- `/recurring` — Fixed monthly bills + auto-detected candidates from recent transactions
- `/rules` — Keyword → category mappings used by `normalize.py`
- `/goals` — Savings goals with required vs. planned monthly contribution
- `/networth` — Monthly net-worth snapshots (assets minus liabilities)

## Project layout

```
app.py                       Flask server with all routes
normalize.py                 CSV ingestion + categorization heuristics
requirements.txt             flask, pandas
templates/                   Jinja2 templates (Tailwind via CDN, Plotly via CDN)
*.example.csv                Starter templates — copy to drop the .example suffix
Transaction CSVs/            Raw bank exports go here (gitignored)
Transactions.csv             Normalized ledger (gitignored)
Categories.csv               Category list with monthly budgets (gitignored)
Goals.csv                    Savings goals (gitignored)
Net Worth.csv                Monthly net-worth snapshots (gitignored)
RecurringBills.csv           Fixed monthly bills (gitignored)
Rules.csv                    Custom keyword→category rules (gitignored)
```

## Privacy

All data stays on your local filesystem as plain CSV. No external services, no telemetry, no auth required. The `.gitignore` excludes every data file so you can develop on the code in this repo without committing your finances.

## Stack

- Python 3.12+, Flask 3, pandas
- Tailwind CSS via CDN, Plotly.js via CDN — no build step
