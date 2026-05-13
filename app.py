"""Personal budget app — local Flask server backed by the CSV files.

Run:
    .venv/bin/python app.py

Then open http://localhost:5000 in your browser.
"""
from pathlib import Path
from datetime import date
import csv
import shutil
from flask import Flask, render_template, request, redirect, url_for, flash
import pandas as pd

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
SAVINGS_CATS = ["Emergency Fund", "Retirement", "Investments"]


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
    df["Progress"] = (df["Current Balance"] / df["Target Amount"]).fillna(0)

    today = pd.Timestamp(date.today())
    months_to_target = ((df["Target Date"] - today).dt.days / 30.4375).clip(lower=0.5)  # min half-month to avoid div-by-zero spikes
    remaining = (df["Target Amount"] - df["Current Balance"]).clip(lower=0)
    df["Required Monthly"] = (remaining / months_to_target).fillna(0)
    df["Shortfall"] = (df["Required Monthly"] - df["Monthly Contribution"]).clip(lower=0)
    df["On Track"] = df["Monthly Contribution"] >= df["Required Monthly"]
    df["Months Remaining"] = ((df["Target Amount"] - df["Current Balance"]) / df["Monthly Contribution"]).replace([float("inf"), -float("inf")], None)
    return df


def load_recurring_bills() -> list[dict]:
    path = DATA / "RecurringBills.csv"
    if not path.exists():
        return []
    with path.open() as f:
        return [
            {"Name": r["Name"], "Amount": float(r["Amount"] or 0),
             "Category": r.get("Category", ""), "Day": r.get("Day", ""),
             "Notes": r.get("Notes", "")}
            for r in csv.DictReader(f)
            if r.get("Name")
        ]


def save_recurring_bills(rows: list[dict]) -> None:
    path = DATA / "RecurringBills.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["Name", "Amount", "Category", "Day", "Notes"])
        w.writeheader()
        for r in rows:
            w.writerow({
                "Name": r["Name"],
                "Amount": f"{float(r.get('Amount') or 0):.2f}",
                "Category": r.get("Category", ""),
                "Day": r.get("Day", ""),
                "Notes": r.get("Notes", ""),
            })


def detect_recurring_candidates(df: pd.DataFrame, months_back: int = 3) -> list[dict]:
    """Find merchants that appear in multiple recent months with similar amounts.

    Excludes income, transfers, savings, and very small charges. Suitable for
    pre-populating the recurring bills list.
    """
    today = pd.Timestamp(date.today())
    cutoff = today - pd.DateOffset(months=months_back)
    recent = df[(df["Date"] >= cutoff) & (df["Amount"] < 0)].copy()
    excluded = INCOME_CATS + SAVINGS_CATS + ["Transfer", "Uncategorized"]
    recent = recent[~recent["Category"].isin(excluded)]
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
                  "Monthly Contribution", "Progress %", "Months Remaining at Current Pace"]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            target = float(r.get("Target Amount") or 0)
            current = float(r.get("Current Balance") or 0)
            monthly = float(r.get("Monthly Contribution") or 0)
            progress = (current / target) if target else 0
            months_left = ((target - current) / monthly) if monthly else ""
            w.writerow({
                "Goal": r["Goal"],
                "Target Amount": f"{target:.2f}",
                "Current Balance": f"{current:.2f}",
                "Target Date": r.get("Target Date", ""),
                "Monthly Contribution": f"{monthly:.2f}",
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

    return render_template(
        "dashboard.html",
        this_month=this_month, ytd=ytd, all_time=all_time,
        cat_spend={k: float(v) for k, v in cat_spend.items()},
        monthly_data=monthly_data,
        nw_data=nw_data,
        goals=goals_list,
        budget_plan=budget_plan,
    )


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
    total = sum(b["Amount"] for b in bills)
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
                "Day": "",
                "Notes": f"Detected from last {s['Months Window']} mo",
            })
            added += 1
    save_recurring_bills(bills)
    flash(f"Added {added} detected bills")
    return redirect(url_for("recurring_view"))


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
