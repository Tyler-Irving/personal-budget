"""Personal budget app — local Flask server backed by the CSV files.

Run:
    .venv/bin/python app.py

Then open http://localhost:5000 in your browser.
"""
from pathlib import Path
from datetime import date, timedelta
from collections import defaultdict
import calendar
import csv
import hashlib
import json
import logging
import os
import shutil
import urllib.error
import urllib.request
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import pandas as pd

import normalize

BASE = Path(__file__).parent
DATA = BASE / "data"
EXAMPLES = BASE / "examples"


def bootstrap_data() -> None:
    """Seed data/ from examples/ on first run.

    Copies any examples/*.csv that doesn't already exist in data/. Never
    overwrites existing user data.
    """
    DATA.mkdir(parents=True, exist_ok=True)
    if EXAMPLES.exists():
        for src in EXAMPLES.glob("*.csv"):
            dst = DATA / src.name
            if not dst.exists():
                shutil.copy(src, dst)


bootstrap_data()

app = Flask(__name__)
app.secret_key = "budget-local-dev"

INCOME_CATS = ["Salary", "Other Income"]
SAVINGS_CATS = ["Emergency Fund", "Retirement"]


def load_transactions() -> pd.DataFrame:
    df = pd.read_csv(DATA / "Transactions.csv")
    df["Date"] = pd.to_datetime(df["Date"])
    df["Amount"] = df["Amount"].astype(float)
    df["Notes"] = df["Notes"].fillna("")
    return df


def save_transactions(df: pd.DataFrame) -> None:
    out = df.copy()
    out["Date"] = out["Date"].dt.strftime("%Y-%m-%d")
    out.to_csv(DATA / "Transactions.csv", index=False, float_format="%.2f")


def load_categories() -> pd.DataFrame:
    return pd.read_csv(DATA / "Categories.csv")


def load_goals() -> pd.DataFrame:
    df = pd.read_csv(DATA / "Goals.csv")
    df["Target Amount"] = df["Target Amount"].astype(float)
    df["Current Balance"] = df["Current Balance"].astype(float)
    df["Monthly Contribution"] = df["Monthly Contribution"].astype(float)
    df["Target Date"] = pd.to_datetime(df["Target Date"], errors="coerce")
    # Day-of-month for forecast scheduling; missing/invalid → 1
    if "Day" in df.columns:
        df["Day"] = pd.to_numeric(df["Day"], errors="coerce").fillna(1).clip(1, 31).astype(int)
    else:
        df["Day"] = 1
    df["Progress"] = (df["Current Balance"] / df["Target Amount"]).fillna(0)

    today = pd.Timestamp(date.today())
    months_to_target = ((df["Target Date"] - today).dt.days / 30.4375).clip(lower=0.5)  # min half-month to avoid div-by-zero spikes
    remaining = (df["Target Amount"] - df["Current Balance"]).clip(lower=0)
    df["Required Monthly"] = (remaining / months_to_target).fillna(0)
    df["Shortfall"] = (df["Required Monthly"] - df["Monthly Contribution"]).clip(lower=0)
    df["On Track"] = df["Monthly Contribution"] >= df["Required Monthly"]
    df["Months Remaining"] = ((df["Target Amount"] - df["Current Balance"]) / df["Monthly Contribution"]).replace([float("inf"), -float("inf")], None)
    return df


VALID_FREQUENCIES = {"monthly", "semi-monthly", "weekly", "biweekly"}
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAY_INDEX = {w.lower(): i for i, w in enumerate(WEEKDAYS)}


def _normalize_frequency(raw: str | None) -> str:
    f = (raw or "").strip().lower()
    return f if f in VALID_FREQUENCIES else "monthly"


def load_recurring_bills() -> list[dict]:
    path = DATA / "RecurringBills.csv"
    if not path.exists():
        return []
    with path.open() as f:
        return [
            {"Name": r["Name"], "Amount": float(r["Amount"] or 0),
             "Category": r.get("Category", ""), "Day": r.get("Day", ""),
             "Frequency": _normalize_frequency(r.get("Frequency")),
             "Notes": r.get("Notes", "")}
            for r in csv.DictReader(f)
            if r.get("Name")
        ]


def save_recurring_bills(rows: list[dict]) -> None:
    path = DATA / "RecurringBills.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["Name", "Amount", "Category", "Frequency", "Day", "Notes"])
        w.writeheader()
        for r in rows:
            w.writerow({
                "Name": r["Name"],
                "Amount": f"{float(r.get('Amount') or 0):.2f}",
                "Category": r.get("Category", ""),
                "Frequency": _normalize_frequency(r.get("Frequency")),
                "Day": r.get("Day", ""),
                "Notes": r.get("Notes", ""),
            })


def is_bill_scheduled(b: dict) -> bool:
    """A bill is 'scheduled' (i.e. forecastable) if its Day field is valid for its Frequency."""
    freq = _normalize_frequency(b.get("Frequency"))
    day = str(b.get("Day", "")).strip()
    if not day:
        return False
    if freq == "monthly":
        return day.isdigit() and 1 <= int(day) <= 31
    if freq == "semi-monthly":
        parts = [p.strip() for p in day.split(",") if p.strip()]
        return len(parts) >= 1 and all(p.isdigit() and 1 <= int(p) <= 31 for p in parts)
    if freq == "weekly":
        return day.lower() in WEEKDAY_INDEX
    if freq == "biweekly":
        try:
            date.fromisoformat(day)
            return True
        except ValueError:
            return False
    return False


def monthly_equivalent(b: dict) -> float:
    """Per-month cost for a recurring bill, regardless of cadence."""
    amount = float(b.get("Amount", 0) or 0)
    if amount <= 0 or not is_bill_scheduled(b):
        return amount
    freq = _normalize_frequency(b.get("Frequency"))
    if freq == "monthly":
        return amount
    if freq == "semi-monthly":
        day = str(b.get("Day", "")).strip()
        n = len([p for p in day.split(",") if p.strip()])
        return amount * n
    if freq == "weekly":
        return amount * (52 / 12)
    if freq == "biweekly":
        return amount * (26 / 12)
    return amount


def bill_occurrences_in_range(b: dict, start: "date", end: "date") -> list["date"]:
    """Enumerate the dates this bill will fire on, inclusive of [start, end]."""
    if not is_bill_scheduled(b) or start > end:
        return []
    freq = _normalize_frequency(b.get("Frequency"))
    day = str(b.get("Day", "")).strip()
    out: list[date] = []
    if freq == "monthly":
        dom = int(day)
        y, m = start.year, start.month
        while True:
            last = calendar.monthrange(y, m)[1]
            d = date(y, m, min(dom, last))
            if d > end:
                break
            if d >= start:
                out.append(d)
            m += 1
            if m > 12:
                m, y = 1, y + 1
        return out
    if freq == "semi-monthly":
        days = sorted({int(p.strip()) for p in day.split(",") if p.strip()})
        y, m = start.year, start.month
        while True:
            last = calendar.monthrange(y, m)[1]
            month_done = True
            for dom in days:
                d = date(y, m, min(dom, last))
                if d > end:
                    continue
                month_done = False
                if d >= start:
                    out.append(d)
            # Stop once an entire month is beyond the horizon
            if date(y, m, 1) > end and month_done:
                break
            m += 1
            if m > 12:
                m, y = 1, y + 1
            if date(y, m, 1) > end:
                break
        return sorted(out)
    if freq == "weekly":
        wd = WEEKDAY_INDEX[day.lower()]
        d = start + timedelta(days=(wd - start.weekday()) % 7)
        while d <= end:
            out.append(d)
            d += timedelta(days=7)
        return out
    if freq == "biweekly":
        anchor = date.fromisoformat(day)
        delta = (start - anchor).days
        # Step forward to the first occurrence >= start
        if delta <= 0:
            d = anchor
        else:
            d = anchor + timedelta(days=((delta + 13) // 14) * 14)
        while d <= end:
            if d >= start:
                out.append(d)
            d += timedelta(days=14)
        return out
    return []


def detect_cadence(df: pd.DataFrame, name: str, fallback_day: int,
                   lookback_days: int = 120) -> tuple[str, str, int]:
    """Infer (frequency, day_field, n_matches) for a merchant from its transaction history.

    Looks at outflows in the last `lookback_days` whose Description contains `name`.
    Falls back to ('monthly', str(fallback_day)) if there's not enough signal.
    """
    today = pd.Timestamp(date.today())
    cutoff = today - pd.Timedelta(days=lookback_days)
    matches = df[
        (df["Date"] >= cutoff)
        & (df["Amount"] < 0)
        & df["Description"].str.contains(name, case=False, na=False, regex=False)
    ].sort_values("Date")

    if len(matches) < 3:
        return "monthly", str(fallback_day), len(matches)

    dates = [d.date() for d in matches["Date"]]
    deltas = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
    deltas_sorted = sorted(deltas)
    median = deltas_sorted[len(deltas_sorted) // 2]

    if 5 <= median <= 9:
        weekdays = [d.weekday() for d in dates]
        mode_wd = max(set(weekdays), key=weekdays.count)
        return "weekly", WEEKDAYS[mode_wd], len(matches)

    if 12 <= median <= 16:
        anchor = dates[-1]
        return "biweekly", anchor.isoformat(), len(matches)

    if median >= 25:
        doms = [d.day for d in dates]
        from collections import Counter
        counts = Counter(doms)
        common = [d for d, c in counts.most_common() if c >= 2]
        if len(common) >= 2:
            top_two = sorted(common[:2])
            return "semi-monthly", ",".join(str(d) for d in top_two), len(matches)
        mode_dom = counts.most_common(1)[0][0]
        return "monthly", str(mode_dom), len(matches)

    return "monthly", str(fallback_day), len(matches)


def describe_bill_cadence(b: dict) -> str:
    """Short human-readable description of a bill's cadence, for flashes."""
    if not is_bill_scheduled(b):
        return "unscheduled"
    freq = _normalize_frequency(b.get("Frequency"))
    day = str(b.get("Day", "")).strip()
    if freq == "monthly":
        return f"monthly on day {day}"
    if freq == "semi-monthly":
        return f"twice-monthly on days {day}"
    if freq == "weekly":
        return f"every {day}"
    if freq == "biweekly":
        return f"biweekly from {day}"
    return freq


def detect_recurring_candidates(df: pd.DataFrame, months_back: int = 3) -> list[dict]:
    """Find merchants tagged 'Bills' that appear in multiple recent months with similar amounts.

    Only considers transactions in the 'Bills' category; other expense categories are
    intentionally out of scope so the bills page stays focused on what the user has
    chosen to track as a bill.
    """
    today = pd.Timestamp(date.today())
    cutoff = today - pd.DateOffset(months=months_back)
    recent = df[(df["Date"] >= cutoff) & (df["Amount"] < 0) & (df["Category"] == "Bills")].copy()
    if recent.empty:
        return []

    def merchant_key(desc: str) -> str:
        if " - " in desc:
            return desc.split(" - ")[0].strip()
        words = desc.split()
        return " ".join(words[:3]) if len(words) >= 3 else desc

    recent["MerchKey"] = recent["Description"].apply(merchant_key)
    recent["YearMonth"] = recent["Date"].dt.to_period("M").astype(str)

    existing_names = {b["Name"].lower() for b in load_recurring_bills()}

    candidates = []
    for key, group in recent.groupby("MerchKey"):
        months_present = group["YearMonth"].nunique()
        if months_present < 2:
            continue
        amounts = group["Amount"].abs()
        avg = float(amounts.mean())
        if avg < 5:
            continue
        if amounts.min() > 0 and amounts.max() / amounts.min() > 3:
            continue
        cat = group["Category"].mode().iloc[0]
        if key.lower() in existing_names:
            continue
        candidates.append({
            "Name": key,
            "Amount": round(avg, 2),
            "Category": cat,
            "Months Present": int(months_present),
            "Months Window": months_back,
            "Sample": group.iloc[0]["Description"][:60],
        })
    return sorted(candidates, key=lambda c: -c["Amount"])


def load_net_worth() -> pd.DataFrame:
    df = pd.read_csv(DATA / "Net Worth.csv")
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    for col in ["Checking", "Savings", "Investments", "Other Assets", "Credit Cards", "Loans"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    df["Total Assets"] = df[["Checking", "Savings", "Investments", "Other Assets"]].sum(axis=1)
    df["Total Liabilities"] = df[["Credit Cards", "Loans"]].sum(axis=1)
    df["Net Worth"] = df["Total Assets"] - df["Total Liabilities"]
    return df.dropna(subset=["Date"]).sort_values("Date")


def load_rules() -> list[dict]:
    path = DATA / "Rules.csv"
    if not path.exists():
        return []
    with path.open() as f:
        return [{"Keyword": r["Keyword"], "Category": r["Category"]}
                for r in csv.DictReader(f)
                if r.get("Keyword") and r.get("Category")]


def save_rules(rules: list[dict]) -> None:
    path = DATA / "Rules.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["Keyword", "Category"])
        w.writeheader()
        w.writerows(rules)


def save_goals(rows: list[dict]) -> None:
    path = DATA / "Goals.csv"
    fieldnames = ["Goal", "Target Amount", "Current Balance", "Target Date",
                  "Monthly Contribution", "Day", "Progress %", "Months Remaining at Current Pace"]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            target = float(r.get("Target Amount") or 0)
            current = float(r.get("Current Balance") or 0)
            monthly = float(r.get("Monthly Contribution") or 0)
            day_raw = str(r.get("Day", "")).strip()
            try:
                day = max(1, min(31, int(day_raw))) if day_raw else 1
            except ValueError:
                day = 1
            progress = (current / target) if target else 0
            months_left = ((target - current) / monthly) if monthly else ""
            w.writerow({
                "Goal": r["Goal"],
                "Target Amount": f"{target:.2f}",
                "Current Balance": f"{current:.2f}",
                "Target Date": r.get("Target Date", ""),
                "Monthly Contribution": f"{monthly:.2f}",
                "Day": day,
                "Progress %": f"{progress:.4f}",
                "Months Remaining at Current Pace": f"{months_left:.1f}" if months_left != "" else "",
            })


def save_net_worth(rows: list[dict]) -> None:
    path = DATA / "Net Worth.csv"
    fieldnames = ["Date", "Checking", "Savings", "Investments", "Other Assets",
                  "Credit Cards", "Loans", "Total Assets", "Total Liabilities", "Net Worth"]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            checking = float(r.get("Checking") or 0)
            savings = float(r.get("Savings") or 0)
            investments = float(r.get("Investments") or 0)
            other = float(r.get("Other Assets") or 0)
            cc = float(r.get("Credit Cards") or 0)
            loans = float(r.get("Loans") or 0)
            assets = checking + savings + investments + other
            liab = cc + loans
            w.writerow({
                "Date": r["Date"],
                "Checking": f"{checking:.2f}",
                "Savings": f"{savings:.2f}",
                "Investments": f"{investments:.2f}",
                "Other Assets": f"{other:.2f}",
                "Credit Cards": f"{cc:.2f}",
                "Loans": f"{loans:.2f}",
                "Total Assets": f"{assets:.2f}",
                "Total Liabilities": f"{liab:.2f}",
                "Net Worth": f"{assets - liab:.2f}",
            })


def summarize(df: pd.DataFrame) -> dict:
    income = float(df[df["Category"].isin(INCOME_CATS)]["Amount"].sum())
    negatives = float(df[df["Amount"] < 0]["Amount"].sum())
    savings_out = float(df[(df["Category"].isin(SAVINGS_CATS)) & (df["Amount"] < 0)]["Amount"].sum())
    transfer_out = float(df[(df["Category"] == "Transfer") & (df["Amount"] < 0)]["Amount"].sum())
    consumption = abs(negatives - savings_out - transfer_out)
    savings = abs(savings_out)
    return {
        "income": income,
        "consumption": consumption,
        "savings": savings,
        "net": income - consumption - savings,
        "savings_rate": (savings / income) if income else 0,
    }


# ----- Forecasting -------------------------------------------------------

def starting_checking_balance(df: pd.DataFrame) -> tuple[float | None, "date | None", str | None]:
    """Today's projected checking balance.

    Reads the latest Net Worth snapshot's Checking value, then applies any
    checking-account transactions that have hit since the snapshot date so
    the forecast starts from a current-as-of-today number.

    Returns (balance, snapshot_date, error_message).
    """
    try:
        nw = load_net_worth()
    except FileNotFoundError:
        return None, None, "No Net Worth.csv yet"
    if nw.empty:
        return None, None, "Net Worth.csv has no rows"

    latest = nw.iloc[-1]
    asset_cols = ["Checking", "Savings", "Investments", "Other Assets", "Credit Cards", "Loans"]
    if all(float(latest[c]) == 0 for c in asset_cols):
        return None, None, "Net Worth.csv has the empty starter row only — fill it in at /networth"

    snapshot_checking = float(latest["Checking"])
    snapshot_date = latest["Date"].date() if hasattr(latest["Date"], "date") else None

    # Roll the snapshot forward to today using transactions on checking accounts
    if snapshot_date is not None:
        since = df[df["Date"].dt.date > snapshot_date]
        checking_txns = since[since["Account"].str.contains("checking", case=False, na=False)]
        delta = float(checking_txns["Amount"].sum()) if not checking_txns.empty else 0.0
    else:
        delta = 0.0

    return snapshot_checking + delta, snapshot_date, None


def detect_income_cadence(df: pd.DataFrame, lookback_days: int = 120) -> dict:
    """Find the user's pay schedule from recent Salary/Other Income history.

    Returns {last_pay, interval_days, avg_amount, cadence, paydays_observed}
    or an empty dict if not enough data.
    """
    income = df[df["Category"].isin(INCOME_CATS) & (df["Amount"] > 0)].copy()
    cutoff = pd.Timestamp(date.today()) - pd.Timedelta(days=lookback_days)
    income = income[income["Date"] >= cutoff]
    if income.empty:
        return {}

    income["DateOnly"] = income["Date"].dt.normalize()
    grouped = income.groupby("DateOnly", as_index=False)["Amount"].sum().sort_values("DateOnly")
    if len(grouped) < 2:
        return {
            "last_pay": grouped["DateOnly"].iloc[-1].date(),
            "interval_days": 30,
            "avg_amount": float(grouped["Amount"].iloc[-1]),
            "cadence": "unknown",
            "paydays_observed": 1,
        }

    gaps = grouped["DateOnly"].diff().dt.days.dropna()
    median_gap = float(gaps.median())

    if 6 <= median_gap <= 8:
        cadence, interval = "weekly", 7
    elif 12 <= median_gap <= 16:
        cadence, interval = "biweekly", 14
    elif 27 <= median_gap <= 32:
        cadence, interval = "monthly", int(round(median_gap))
    else:
        cadence, interval = "irregular", int(round(median_gap))

    return {
        "last_pay": grouped["DateOnly"].iloc[-1].date(),
        "interval_days": interval,
        "avg_amount": float(grouped["Amount"].mean()),
        "cadence": cadence,
        "paydays_observed": len(grouped),
    }


def average_daily_variable_burn(df: pd.DataFrame, bills: list[dict],
                                months_back: int = 3, mode: str = "mean") -> float:
    """Daily $ burn rate for everything that isn't a scheduled bill, income, or savings.

    Uses last `months_back` *full* months. Mode "p75" picks the 75th-percentile
    month for a more conservative forecast; "mean" averages.
    Scheduled bills (those with a Day set) are subtracted because they're
    forecasted as discrete events; counting them in both would double-bill.
    """
    today = pd.Timestamp(date.today())
    month_start = today.replace(day=1)
    cutoff = month_start - pd.DateOffset(months=months_back)
    recent = df[(df["Date"] >= cutoff) & (df["Date"] < month_start)]
    excluded = INCOME_CATS + SAVINGS_CATS + ["Transfer"]
    outflows = recent[(~recent["Category"].isin(excluded)) & (recent["Amount"] < 0)]
    if outflows.empty:
        return 0.0

    n_months = max(1, outflows["Date"].dt.to_period("M").nunique())
    if mode == "p75":
        monthly = outflows.groupby(outflows["Date"].dt.to_period("M"))["Amount"].sum().abs()
        baseline_monthly = float(monthly.quantile(0.75)) if not monthly.empty else 0.0
    else:
        baseline_monthly = float(outflows["Amount"].abs().sum() / n_months)

    scheduled_bills_total = sum(monthly_equivalent(b) for b in bills if is_bill_scheduled(b))
    variable_monthly = max(0.0, baseline_monthly - scheduled_bills_total)
    return variable_monthly / 30.44


def _monthly_events_in_horizon(start: "date", horizon_end: "date",
                               day_of_month: int, label: str,
                               amount: float, event_type: str) -> list[dict]:
    """Emit one event per month at day_of_month, clamped to month length, from start..horizon_end."""
    events = []
    year, month = start.year, start.month
    while True:
        last_day = calendar.monthrange(year, month)[1]
        d = date(year, month, min(day_of_month, last_day))
        if d > horizon_end:
            break
        if d >= start:
            events.append({"date": d, "label": label, "amount": amount, "type": event_type})
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return events


def build_forecast(horizon_days: int = 180, include_savings: bool = True,
                   mode: str = "mean") -> dict:
    """Walk day-by-day projecting checking balance forward."""
    df = load_transactions()
    goals_df = load_goals()
    bills = load_recurring_bills()
    today = date.today()
    horizon_end = today + timedelta(days=horizon_days)

    warnings: list[str] = []

    start_balance, snapshot_date, err = starting_checking_balance(df)
    if start_balance is None:
        warnings.append(err or "No starting balance available.")
        start_balance = 0.0

    income_info = detect_income_cadence(df)
    income_events: list[dict] = []
    if income_info:
        d = income_info["last_pay"] + timedelta(days=income_info["interval_days"])
        while d <= horizon_end:
            if d >= today:
                income_events.append({
                    "date": d, "label": "Paycheck",
                    "amount": income_info["avg_amount"], "type": "income",
                })
            d += timedelta(days=income_info["interval_days"])
    else:
        warnings.append("No paychecks in the last 120 days — add income history for an accurate forecast.")

    bill_events: list[dict] = []
    bills_no_day: list[str] = []
    for b in bills:
        if not is_bill_scheduled(b):
            bills_no_day.append(b["Name"])
            continue
        amount = -float(b["Amount"])
        for d in bill_occurrences_in_range(b, today, horizon_end):
            bill_events.append({"date": d, "label": b["Name"], "amount": amount, "type": "bill"})
    if bills_no_day:
        sample = ", ".join(bills_no_day[:3])
        more = f" (+{len(bills_no_day) - 3} more)" if len(bills_no_day) > 3 else ""
        warnings.append(f"{len(bills_no_day)} bill(s) have no schedule set, excluded from event timeline: {sample}{more}")

    savings_events: list[dict] = []
    if include_savings and not goals_df.empty:
        for _, g in goals_df.iterrows():
            monthly = float(g["Monthly Contribution"])
            if monthly <= 0:
                continue
            day = int(g["Day"]) if pd.notna(g.get("Day")) else 1
            savings_events.extend(_monthly_events_in_horizon(
                today, horizon_end, day, f"→ {g['Goal']}", -monthly, "savings",
            ))

    daily_burn = average_daily_variable_burn(df, bills, mode=mode)

    by_date: dict = defaultdict(list)
    for e in income_events + bill_events + savings_events:
        by_date[e["date"]].append(e)

    dates: list[str] = []
    balances: list[float] = []
    balance = start_balance
    low_point = {"date": today, "balance": round(balance, 2)}
    negative_days = 0

    for i in range(horizon_days + 1):
        d = today + timedelta(days=i)
        for e in by_date.get(d, []):
            balance += e["amount"]
        if i > 0:
            balance -= daily_burn
        dates.append(d.strftime("%Y-%m-%d"))
        balances.append(round(balance, 2))
        if balance < low_point["balance"]:
            low_point = {"date": d, "balance": round(balance, 2)}
        if balance < 0:
            negative_days += 1

    all_events = sorted(income_events + bill_events + savings_events, key=lambda e: e["date"])

    # Compute running balance at each event, useful for hover text on the chart
    date_to_balance = dict(zip(dates, balances))

    return {
        "start_balance": round(start_balance, 2),
        "start_date": snapshot_date.strftime("%Y-%m-%d") if snapshot_date else None,
        "snapshot_age_days": (today - snapshot_date).days if snapshot_date else None,
        "dates": dates,
        "balances": balances,
        "events": [{
            "date": e["date"].strftime("%Y-%m-%d"),
            "label": e["label"],
            "amount": round(e["amount"], 2),
            "type": e["type"],
            "balance_after": date_to_balance.get(e["date"].strftime("%Y-%m-%d"), 0),
        } for e in all_events],
        "low_point": {
            "date": low_point["date"].strftime("%Y-%m-%d") if hasattr(low_point["date"], "strftime") else str(low_point["date"]),
            "balance": low_point["balance"],
        },
        "negative_days": negative_days,
        "daily_burn": round(daily_burn, 2),
        "horizon_days": horizon_days,
        "include_savings": include_savings,
        "mode": mode,
        "warnings": warnings,
        "income": income_info,
    }


# ----- Pace tracker ------------------------------------------------------

def compute_pace(df: pd.DataFrame, today: "date",
                 bills: list[dict], cats_df: pd.DataFrame) -> dict | None:
    """Per-category month-to-date pace vs typical (or vs budget if set).

    Bills are subtracted from both actual and baseline so pace tracks
    *discretionary* spending only — otherwise paying rent on the 1st makes
    Rent always look like it's running 200%+.

    Returns None for the first 4 days of the month (pace is too noisy there).
    """
    excluded = set(INCOME_CATS + SAVINGS_CATS + ["Transfer", "Uncategorized"])

    month_start = today.replace(day=1)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    days_elapsed = today.day
    if days_elapsed < 5:
        return None

    today_ts = pd.Timestamp(today)
    month_start_ts = pd.Timestamp(month_start)
    mtd = df[(df["Date"] >= month_start_ts) & (df["Date"] <= today_ts) & (df["Amount"] < 0)]
    mtd = mtd[~mtd["Category"].isin(excluded)]
    actual_by_cat = mtd.groupby("Category")["Amount"].sum().abs().to_dict()

    cutoff = month_start_ts - pd.DateOffset(months=3)
    hist = df[(df["Date"] >= cutoff) & (df["Date"] < month_start_ts) & (df["Amount"] < 0)]
    hist = hist[~hist["Category"].isin(excluded)]
    n_months = hist["Date"].dt.to_period("M").nunique() if not hist.empty else 0
    if n_months == 0:
        avg_by_cat: dict = {}
    else:
        avg_by_cat = (hist.groupby("Category")["Amount"].sum().abs() / n_months).to_dict()

    budget_by_cat: dict = {}
    if "Monthly Budget" in cats_df.columns:
        for _, row in cats_df.iterrows():
            try:
                v = float(row.get("Monthly Budget", 0) or 0)
            except (ValueError, TypeError):
                v = 0
            if v > 0:
                budget_by_cat[row["Category"]] = v

    bills_by_cat: dict = defaultdict(list)
    for b in bills:
        cat = (b.get("Category") or "").strip()
        if not cat:
            continue
        bills_by_cat[cat].append(b)

    rows = []
    for cat in set(actual_by_cat) | set(avg_by_cat) | set(budget_by_cat):
        baseline_monthly = budget_by_cat.get(cat) or avg_by_cat.get(cat, 0)
        source = "budget" if cat in budget_by_cat else "avg"
        if baseline_monthly <= 0:
            continue

        cat_bills = bills_by_cat.get(cat, [])
        cat_bill_total = sum(monthly_equivalent(b) for b in cat_bills)
        cat_bill_paid_so_far = sum(
            float(b.get("Amount", 0) or 0) * len(bill_occurrences_in_range(b, month_start, today))
            for b in cat_bills if is_bill_scheduled(b)
        )

        baseline_discretionary = max(0.0, baseline_monthly - cat_bill_total)
        if baseline_discretionary <= 0:
            continue

        actual_total = actual_by_cat.get(cat, 0.0)
        actual_discretionary = max(0.0, actual_total - cat_bill_paid_so_far)
        expected_so_far = baseline_discretionary * (days_elapsed / days_in_month)
        if expected_so_far <= 0:
            continue

        # Hide tiny categories (less than $5 expected by now and $0 spent) to reduce noise
        if expected_so_far < 5 and actual_discretionary == 0:
            continue

        pace_pct = actual_discretionary / expected_so_far
        if pace_pct > 1.2:
            bucket = "hot"
        elif pace_pct < 0.6:
            bucket = "cold"
        else:
            bucket = "normal"

        rows.append({
            "category": cat,
            "actual": round(actual_discretionary, 2),
            "expected": round(expected_so_far, 2),
            "baseline_monthly": round(baseline_discretionary, 2),
            "pace_pct": round(pace_pct, 2),
            "bucket": bucket,
            "source": source,
        })

    rows.sort(key=lambda r: -abs(r["pace_pct"] - 1.0))

    return {
        "rows": rows,
        "days_elapsed": days_elapsed,
        "days_in_month": days_in_month,
        "hot": [r for r in rows if r["bucket"] == "hot"],
        "normal": [r for r in rows if r["bucket"] == "normal"],
        "cold": [r for r in rows if r["bucket"] == "cold"],
    }


# ----- Briefing (local LLM via Ollama) -----------------------------------

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "30"))
BRIEFING_CACHE = DATA / "briefing_cache.json"

BRIEFING_SYSTEM_PROMPT = (
    "You are a personal-finance briefer. Write 2-4 sentences in a terse "
    "financial-analyst style. Lead with whatever is most urgent. Be specific: "
    "cite dollar amounts and dates from the data. Don't moralize. Don't say "
    "\"consider\" or \"you might want to\". State facts, not advice. Mention "
    "good news only when a category is meaningfully under pace. No bullets, "
    "no headers, no preamble — just one paragraph."
)


def _merchant_key(desc: str) -> str:
    if " - " in desc:
        return desc.split(" - ")[0].strip().upper()
    words = desc.split()
    return " ".join(words[:3]).upper() if len(words) >= 3 else desc.strip().upper()


def detect_anomalies(df: pd.DataFrame, bills: list[dict], today: "date") -> dict:
    """Flag bill drift, new merchants, large charges, and missing bills."""
    today_ts = pd.Timestamp(today)
    month_start_ts = today_ts.normalize().replace(day=1)

    mtd = df[(df["Date"] >= month_start_ts) & (df["Date"] <= today_ts) & (df["Amount"] < 0)]
    mtd = mtd[~mtd["Category"].isin(["Transfer"])]
    hist = df[df["Date"] < month_start_ts]

    bill_drift = []
    for b in bills:
        expected = float(b.get("Amount", 0) or 0)
        if expected <= 0:
            continue
        name = b["Name"]
        cutoff = month_start_ts - pd.DateOffset(months=4)
        matches = df[
            (df["Date"] >= cutoff)
            & df["Description"].str.contains(name, case=False, na=False, regex=False)
            & (df["Amount"] < 0)
        ]
        if matches.empty:
            continue
        latest = matches.sort_values("Date", ascending=False).iloc[0]
        latest_amt = abs(float(latest["Amount"]))
        drift = (latest_amt - expected) / expected
        if abs(drift) > 0.05:
            bill_drift.append({
                "name": name,
                "configured": round(expected, 2),
                "latest": round(latest_amt, 2),
                "drift_pct": round(drift * 100, 1),
                "date": latest["Date"].strftime("%Y-%m-%d"),
            })

    new_merchants = []
    if not mtd.empty:
        mtd_keys = set(mtd["Description"].apply(_merchant_key))
        hist_keys = set(hist["Description"].apply(_merchant_key)) if not hist.empty else set()
        for key in mtd_keys - hist_keys:
            m = mtd[mtd["Description"].apply(_merchant_key) == key]
            total = float(m["Amount"].abs().sum())
            new_merchants.append({
                "key": key,
                "sample": m.iloc[0]["Description"][:60],
                "total": round(total, 2),
                "count": int(len(m)),
            })
        new_merchants.sort(key=lambda m: -m["total"])

    cutoff6 = month_start_ts - pd.DateOffset(months=6)
    hist6 = df[(df["Date"] >= cutoff6) & (df["Date"] < month_start_ts) & (df["Amount"] < 0)]
    hist6 = hist6[~hist6["Category"].isin(["Transfer"])]
    p95 = float(hist6["Amount"].abs().quantile(0.95)) if not hist6.empty else 0
    large = mtd[mtd["Amount"].abs() > p95] if p95 > 0 else pd.DataFrame()
    large_charges = [{
        "date": r["Date"].strftime("%Y-%m-%d"),
        "description": r["Description"][:60],
        "amount": round(abs(float(r["Amount"])), 2),
    } for _, r in large.sort_values("Amount").head(5).iterrows()]

    missing_bills = []
    month_start = today.replace(day=1)
    for b in bills:
        if not is_bill_scheduled(b):
            continue
        occurrences_due = bill_occurrences_in_range(b, month_start, today)
        if not occurrences_due:
            continue
        name = b["Name"]
        matches = mtd[mtd["Description"].str.contains(name, case=False, na=False, regex=False)]
        if len(matches) < len(occurrences_due):
            missing_bills.append({
                "name": name,
                "expected_day": occurrences_due[0].day,
                "expected_count": len(occurrences_due),
                "actual_count": int(len(matches)),
                "amount": round(float(b.get("Amount", 0) or 0), 2),
            })

    return {
        "bill_drift": bill_drift,
        "new_merchants": new_merchants[:10],
        "large_charges": large_charges,
        "missing_bills": missing_bills,
        "p95_charge": round(p95, 2),
    }


def ollama_generate(user_prompt: str, system: str) -> tuple[str | None, str | None]:
    """Returns (text, error). On any failure, text is None and error is a short message."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "options": {"temperature": 0.2},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            text = body.get("message", {}).get("content", "").strip()
            return (text or None), (None if text else "Ollama returned an empty response")
    except urllib.error.URLError as e:
        return None, f"Ollama unreachable at {OLLAMA_URL} ({e.reason})"
    except (TimeoutError, ConnectionError) as e:
        return None, f"Ollama timed out: {e}"
    except (json.JSONDecodeError, KeyError) as e:
        return None, f"Ollama returned malformed JSON: {e}"


def _briefing_cache_read() -> dict:
    if not BRIEFING_CACHE.exists():
        return {}
    try:
        return json.loads(BRIEFING_CACHE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _briefing_cache_write(entry: dict) -> None:
    try:
        BRIEFING_CACHE.write_text(json.dumps(entry))
    except OSError as e:
        app.logger.warning("Failed to write briefing cache: %s", e)


def generate_briefing(today: "date", force: bool = False) -> dict:
    """Compose a one-paragraph briefing. Cached per (date, input-hash).

    Returns {text, source, error, model}.
    source: 'cache' | 'fresh' | 'none'
    """
    try:
        df = load_transactions()
    except FileNotFoundError:
        return {"text": None, "source": "none", "error": "No transactions yet.", "model": OLLAMA_MODEL}

    bills = load_recurring_bills()
    cats_df = load_categories()

    forecast = build_forecast(horizon_days=60, include_savings=True, mode="mean")
    pace = compute_pace(df, today, bills, cats_df)
    anomalies = detect_anomalies(df, bills, today)

    horizon_end = (today + timedelta(days=14)).isoformat()
    next_events = [e for e in forecast["events"] if e["date"] <= horizon_end][:8]

    payload = {
        "today": today.isoformat(),
        "checking_balance_now": forecast["start_balance"],
        "forecast_low_point_60d": forecast["low_point"],
        "forecast_negative_days_60d": forecast["negative_days"],
        "next_14d_scheduled": [
            {"date": e["date"], "label": e["label"], "amount": e["amount"]} for e in next_events
        ],
        "pace_hot": [
            {"category": r["category"], "actual": r["actual"],
             "expected": r["expected"], "pct_of_expected": int(r["pace_pct"] * 100)}
            for r in (pace["hot"] if pace else [])
        ],
        "pace_cold": [
            {"category": r["category"], "actual": r["actual"],
             "expected": r["expected"], "pct_of_expected": int(r["pace_pct"] * 100)}
            for r in (pace["cold"] if pace else [])
        ][:3],
        "bill_drift": anomalies["bill_drift"][:3],
        "new_merchants_this_month": [
            {"name": m["sample"], "total": m["total"]} for m in anomalies["new_merchants"][:3]
        ],
        "missing_bills_overdue": anomalies["missing_bills"][:3],
    }

    input_str = json.dumps(payload, sort_keys=True)
    input_hash = hashlib.sha256(input_str.encode()).hexdigest()[:16]

    cache = _briefing_cache_read()
    entry = cache.get("entry") if isinstance(cache, dict) else None
    if not force and entry and entry.get("date") == today.isoformat() and entry.get("hash") == input_hash:
        return {"text": entry.get("text"), "source": "cache", "error": None, "model": entry.get("model", OLLAMA_MODEL)}

    user_prompt = (
        "Today's financial snapshot (JSON). Write the briefing as instructed:\n\n"
        + json.dumps(payload, indent=2)
    )
    text, err = ollama_generate(user_prompt, BRIEFING_SYSTEM_PROMPT)
    if not text:
        return {"text": None, "source": "none", "error": err, "model": OLLAMA_MODEL}

    _briefing_cache_write({
        "entry": {
            "date": today.isoformat(),
            "hash": input_hash,
            "text": text,
            "model": OLLAMA_MODEL,
            "generated_at": pd.Timestamp.now().isoformat(),
        }
    })
    return {"text": text, "source": "fresh", "error": None, "model": OLLAMA_MODEL}


@app.context_processor
def inject_unc_count():
    try:
        df = load_transactions()
        return {"unc_count": int((df["Category"] == "Uncategorized").sum())}
    except Exception:
        return {"unc_count": 0}


@app.route("/")
def dashboard():
    df = load_transactions()
    cats_df = load_categories()
    goals_df = load_goals()
    nw_df = load_net_worth()
    bills = load_recurring_bills()

    today = pd.Timestamp(date.today())
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)

    this_month = summarize(df[(df["Date"] >= month_start) & (df["Date"] <= today)])
    ytd = summarize(df[(df["Date"] >= year_start) & (df["Date"] <= today)])
    all_time = summarize(df)

    # Budget Plan: avg income (last 3 calendar months, excluding current) vs fixed obligations
    three_mo_ago = (month_start - pd.DateOffset(months=3))
    last_3 = df[(df["Date"] >= three_mo_ago) & (df["Date"] < month_start) & df["Category"].isin(INCOME_CATS)]
    avg_income = float(last_3["Amount"].sum() / 3) if not last_3.empty else 0.0
    fixed_bills_total = sum(b["Amount"] for b in bills)
    required_savings_total = float(goals_df["Required Monthly"].sum())
    available = avg_income - fixed_bills_total - required_savings_total
    budget_plan = {
        "income": avg_income,
        "fixed": fixed_bills_total,
        "required_savings": required_savings_total,
        "available": available,
        "n_bills": len(bills),
        "n_goals": len(goals_df),
    }

    ytd_df = df[(df["Date"] >= year_start) & (df["Date"] <= today)]
    cat_spend = (
        ytd_df[(ytd_df["Amount"] < 0) & (~ytd_df["Category"].isin(["Transfer"]))]
        .groupby("Category")["Amount"].sum().abs()
        .sort_values(ascending=True)
    )

    df_recent = df[df["Date"] >= today - pd.Timedelta(days=365)].copy()
    df_recent["Month"] = df_recent["Date"].dt.to_period("M").astype(str)
    income_by_month = df_recent[df_recent["Category"].isin(INCOME_CATS)].groupby("Month")["Amount"].sum()
    expense_mask = (~df_recent["Category"].isin(INCOME_CATS + SAVINGS_CATS + ["Transfer"])) & (df_recent["Amount"] < 0)
    expense_by_month = df_recent[expense_mask].groupby("Month")["Amount"].sum().abs()
    savings_mask = (df_recent["Category"].isin(SAVINGS_CATS)) & (df_recent["Amount"] < 0)
    savings_by_month = df_recent[savings_mask].groupby("Month")["Amount"].sum().abs()

    months = sorted(set(income_by_month.index) | set(expense_by_month.index) | set(savings_by_month.index))
    monthly_data = {
        "months": months,
        "income": [float(income_by_month.get(m, 0)) for m in months],
        "expenses": [float(expense_by_month.get(m, 0)) for m in months],
        "savings": [float(savings_by_month.get(m, 0)) for m in months],
    }

    nw_data = {
        "dates": nw_df["Date"].dt.strftime("%Y-%m-%d").tolist(),
        "values": nw_df["Net Worth"].tolist(),
    }

    goals_list = []
    for _, g in goals_df.iterrows():
        goals_list.append({
            "name": g["Goal"],
            "current": float(g["Current Balance"]),
            "target": float(g["Target Amount"]),
            "progress": float(g["Progress"]),
        })

    pace = compute_pace(df, today.date(), bills, cats_df)
    briefing = generate_briefing(today.date())

    return render_template(
        "dashboard.html",
        this_month=this_month, ytd=ytd, all_time=all_time,
        cat_spend={k: float(v) for k, v in cat_spend.items()},
        monthly_data=monthly_data,
        nw_data=nw_data,
        goals=goals_list,
        budget_plan=budget_plan,
        pace=pace,
        briefing=briefing,
    )


@app.route("/briefing/refresh", methods=["POST"])
def briefing_refresh():
    today = date.today()
    result = generate_briefing(today, force=True)
    if result.get("error"):
        flash(f"Briefing refresh failed: {result['error']}")
    else:
        flash("Briefing refreshed.")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/categories/budget", methods=["POST"])
def categories_budget():
    cat = request.form.get("category", "").strip()
    raw = request.form.get("budget", "").strip()
    if not cat:
        return redirect(request.referrer or url_for("dashboard"))
    try:
        budget = max(0.0, float(raw)) if raw else 0.0
    except ValueError:
        budget = 0.0

    path = DATA / "Categories.csv"
    cats = pd.read_csv(path)
    if "Monthly Budget" not in cats.columns:
        cats["Monthly Budget"] = 0
    if cat in cats["Category"].values:
        cats.loc[cats["Category"] == cat, "Monthly Budget"] = budget
        cats.to_csv(path, index=False)
        flash(f"Budget for {cat}: ${budget:,.0f}/mo" if budget else f"Cleared budget for {cat}")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/forecast")
def forecast_view():
    horizon = request.args.get("horizon", "180")
    horizon_days = int(horizon) if horizon.isdigit() and int(horizon) in (30, 90, 180, 365) else 180
    include_savings = request.args.get("include_savings", "on") == "on"
    mode = request.args.get("mode", "mean")
    if mode not in ("mean", "p75"):
        mode = "mean"

    forecast = build_forecast(
        horizon_days=horizon_days,
        include_savings=include_savings,
        mode=mode,
    )
    return render_template("forecast.html", f=forecast)


@app.route("/transactions")
def transactions():
    df = load_transactions()
    cats_df = load_categories()
    categories = cats_df["Category"].tolist()

    cat_filter = request.args.get("category", "")
    search = request.args.get("search", "").strip().lower()
    account_filter = request.args.get("account", "")

    filtered = df.copy()
    if cat_filter:
        filtered = filtered[filtered["Category"] == cat_filter]
    if account_filter:
        filtered = filtered[filtered["Account"] == account_filter]
    if search:
        filtered = filtered[filtered["Description"].str.lower().str.contains(search, na=False)]

    filtered = filtered.sort_values("Date", ascending=False)

    page = int(request.args.get("page", 1))
    per_page = 100
    total = len(filtered)
    start = (page - 1) * per_page
    end = start + per_page
    page_rows = filtered.iloc[start:end].copy()
    page_rows["_idx"] = page_rows.index

    accounts = sorted(df["Account"].unique().tolist())

    return render_template(
        "transactions.html",
        rows=page_rows.to_dict("records"),
        categories=categories,
        accounts=accounts,
        cat_filter=cat_filter,
        account_filter=account_filter,
        search=search,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=max(1, (total + per_page - 1) // per_page),
    )


@app.route("/categorize", methods=["POST"])
def categorize():
    df = load_transactions()
    cat = request.form.get("category", "").strip()
    if not cat:
        return redirect(request.referrer or url_for("transactions"))

    if "idx" in request.form:
        idx = int(request.form["idx"])
        if idx in df.index:
            df.at[idx, "Category"] = cat
            save_transactions(df)
    elif "indices" in request.form:
        indices = [int(i) for i in request.form.getlist("indices")]
        valid = [i for i in indices if i in df.index]
        if valid:
            df.loc[valid, "Category"] = cat
            save_transactions(df)
            flash(f"Updated {len(valid)} transactions to {cat}")
    elif "pattern" in request.form:
        pattern = request.form["pattern"].strip()
        save_rule = request.form.get("save_rule") == "on"
        if pattern:
            mask = (df["Category"] == "Uncategorized") & df["Description"].str.contains(
                pattern, case=False, na=False, regex=False
            )
            n_changed = int(mask.sum())
            df.loc[mask, "Category"] = cat
            save_transactions(df)
            if save_rule:
                rules = load_rules()
                # don't duplicate
                if not any(r["Keyword"].upper() == pattern.upper() for r in rules):
                    rules.append({"Keyword": pattern.upper(), "Category": cat})
                    save_rules(rules)
                    flash(f"Updated {n_changed} transactions and saved rule: '{pattern.upper()}' → {cat}")
                else:
                    flash(f"Updated {n_changed} transactions (rule already existed)")
            else:
                flash(f"Updated {n_changed} transactions")

    return redirect(request.referrer or url_for("transactions"))


# ----- Rules -----

@app.route("/rules")
def rules_view():
    rules = load_rules()
    categories = load_categories()["Category"].tolist()
    return render_template("rules.html", rules=rules, categories=categories)


@app.route("/rules/add", methods=["POST"])
def rules_add():
    keyword = request.form.get("keyword", "").strip().upper()
    cat = request.form.get("category", "").strip()
    if keyword and cat:
        rules = load_rules()
        if not any(r["Keyword"].upper() == keyword for r in rules):
            rules.append({"Keyword": keyword, "Category": cat})
            save_rules(rules)
            flash(f"Added rule: '{keyword}' → {cat}")
        else:
            flash("Rule with that keyword already exists")
    return redirect(url_for("rules_view"))


@app.route("/rules/delete", methods=["POST"])
def rules_delete():
    keyword = request.form.get("keyword", "").strip()
    rules = [r for r in load_rules() if r["Keyword"] != keyword]
    save_rules(rules)
    flash(f"Deleted rule: '{keyword}'")
    return redirect(url_for("rules_view"))


# ----- Goals -----

@app.route("/goals")
def goals_view():
    goals_df = load_goals()
    return render_template("goals.html", goals=goals_df.to_dict("records"))


@app.route("/goals/save", methods=["POST"])
def goals_save():
    n = int(request.form.get("count", 0))
    rows = []
    for i in range(n):
        name = request.form.get(f"goal_{i}", "").strip()
        if not name:
            continue
        rows.append({
            "Goal": name,
            "Target Amount": request.form.get(f"target_{i}") or 0,
            "Current Balance": request.form.get(f"current_{i}") or 0,
            "Target Date": request.form.get(f"date_{i}", "").strip(),
            "Monthly Contribution": request.form.get(f"monthly_{i}") or 0,
            "Day": request.form.get(f"day_{i}", "").strip(),
        })
    save_goals(rows)
    flash(f"Saved {len(rows)} goals")
    return redirect(url_for("goals_view"))


# ----- Net Worth -----

@app.route("/networth")
def networth_view():
    nw_df = load_net_worth()
    nw_df["Date"] = nw_df["Date"].dt.strftime("%Y-%m-%d")
    return render_template("networth.html", rows=nw_df.to_dict("records"))


@app.route("/networth/save", methods=["POST"])
def networth_save():
    n = int(request.form.get("count", 0))
    rows = []
    for i in range(n):
        d = request.form.get(f"date_{i}", "").strip()
        if not d:
            continue
        rows.append({
            "Date": d,
            "Checking": request.form.get(f"checking_{i}") or 0,
            "Savings": request.form.get(f"savings_{i}") or 0,
            "Investments": request.form.get(f"investments_{i}") or 0,
            "Other Assets": request.form.get(f"other_{i}") or 0,
            "Credit Cards": request.form.get(f"cc_{i}") or 0,
            "Loans": request.form.get(f"loans_{i}") or 0,
        })
    rows.sort(key=lambda r: r["Date"])
    save_net_worth(rows)
    flash(f"Saved {len(rows)} net worth snapshots")
    return redirect(url_for("networth_view"))


# ----- Recurring Bills -----

@app.route("/recurring")
def recurring_view():
    bills = load_recurring_bills()
    df = load_transactions()
    suggestions = detect_recurring_candidates(df)
    categories = load_categories()["Category"].tolist()
    for b in bills:
        b["MonthlyEquivalent"] = monthly_equivalent(b)
    total = sum(b["MonthlyEquivalent"] for b in bills)
    return render_template(
        "recurring.html",
        bills=bills,
        suggestions=suggestions,
        categories=categories,
        total=total,
    )


@app.route("/recurring/save", methods=["POST"])
def recurring_save():
    n = int(request.form.get("count", 0))
    rows = []
    for i in range(n):
        name = request.form.get(f"name_{i}", "").strip()
        if not name:
            continue
        rows.append({
            "Name": name,
            "Amount": request.form.get(f"amount_{i}") or 0,
            "Category": request.form.get(f"category_{i}", "").strip(),
            "Frequency": request.form.get(f"frequency_{i}", "monthly").strip(),
            "Day": request.form.get(f"day_{i}", "").strip(),
            "Notes": request.form.get(f"notes_{i}", "").strip(),
        })
    save_recurring_bills(rows)
    flash(f"Saved {len(rows)} recurring bills")
    return redirect(url_for("recurring_view"))


@app.route("/recurring/add-detected", methods=["POST"])
def recurring_add_detected():
    selected = request.form.getlist("selected")
    if not selected:
        return redirect(url_for("recurring_view"))
    df = load_transactions()
    suggestions = {c["Name"]: c for c in detect_recurring_candidates(df)}
    bills = load_recurring_bills()
    existing_names = {b["Name"].lower() for b in bills}
    added = 0
    for name in selected:
        if name in suggestions and name.lower() not in existing_names:
            s = suggestions[name]
            bills.append({
                "Name": s["Name"],
                "Amount": s["Amount"],
                "Category": s["Category"],
                "Frequency": "monthly",
                "Day": "",
                "Notes": f"Detected from last {s['Months Window']} mo",
            })
            added += 1
    save_recurring_bills(bills)
    flash(f"Added {added} detected bills")
    return redirect(url_for("recurring_view"))


@app.route("/recurring/add-from-transaction", methods=["POST"])
def recurring_add_from_transaction():
    """Promote a single transaction to a recurring bill with sensible defaults."""
    try:
        idx = int(request.form.get("idx", "-1"))
    except ValueError:
        idx = -1
    df = load_transactions()
    if idx < 0 or idx not in df.index:
        flash("Transaction not found.")
        return redirect(request.referrer or url_for("transactions"))

    row = df.loc[idx]
    name = str(row["Description"]).strip()[:60]
    amount = abs(float(row["Amount"]))
    fallback_day = int(row["Date"].day)
    category = str(row["Category"]) if pd.notna(row["Category"]) else ""

    bills = load_recurring_bills()
    if any(b["Name"].lower() == name.lower() for b in bills):
        flash(f"'{name}' is already a recurring bill — edit it at /recurring.")
        return redirect(request.referrer or url_for("transactions"))

    # Use the merchant key (first segment) for cadence detection — broader match than full description
    key = name.split(" - ")[0].strip() if " - " in name else " ".join(name.split()[:3])
    frequency, day_field, n_matches = detect_cadence(df, key, fallback_day)

    bill = {
        "Name": name,
        "Amount": amount,
        "Category": category,
        "Frequency": frequency,
        "Day": day_field,
        "Notes": f"From {row['Date'].strftime('%Y-%m-%d')} transaction ({n_matches} matches in last 120d)",
    }
    bills.append(bill)
    save_recurring_bills(bills)
    flash(f"Added '{name}' (${amount:,.2f}, {describe_bill_cadence(bill)}) to recurring bills — review at /recurring.")
    return redirect(request.referrer or url_for("transactions"))


# ----- Monthly drilldown -----

@app.route("/month/<yyyymm>")
def month_view(yyyymm: str):
    try:
        year, month = map(int, yyyymm.split("-"))
    except ValueError:
        return redirect(url_for("dashboard"))

    df = load_transactions()
    df["Month"] = df["Date"].dt.strftime("%Y-%m")
    month_df = df[df["Month"] == yyyymm].copy()
    summary = summarize(month_df)

    # By category, expenses only
    cat_break = (
        month_df[(month_df["Amount"] < 0) & (~month_df["Category"].isin(["Transfer"]))]
        .groupby("Category")["Amount"].sum().abs()
        .sort_values(ascending=False)
    )

    # Top merchants this month (top 10 by absolute spending)
    top_merch = (
        month_df[(month_df["Amount"] < 0) & (~month_df["Category"].isin(["Transfer"]))]
        .groupby("Description")["Amount"].sum().abs()
        .sort_values(ascending=False).head(10)
    )

    # All transactions, sorted desc by date
    rows = month_df.sort_values("Date", ascending=False).copy()
    rows["_idx"] = rows.index

    # Available months for nav
    all_months = sorted(df["Month"].unique().tolist())
    idx = all_months.index(yyyymm) if yyyymm in all_months else -1
    prev_month = all_months[idx - 1] if idx > 0 else None
    next_month = all_months[idx + 1] if 0 <= idx < len(all_months) - 1 else None

    categories = load_categories()["Category"].tolist()

    return render_template(
        "month.html",
        yyyymm=yyyymm,
        summary=summary,
        cat_break=cat_break.to_dict(),
        top_merch=top_merch.to_dict(),
        rows=rows.to_dict("records"),
        categories=categories,
        prev_month=prev_month,
        next_month=next_month,
        all_months=all_months,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000, host="127.0.0.1")
