"""Normalize bank/credit card CSVs into the budget Transactions schema.

Output schema: Date (YYYY-MM-DD), Description, Amount (signed), Account, Category, Notes
- Amount sign convention: positive = inflow, negative = outflow (from the user's perspective).
  For credit cards: a purchase is -X (spending); a payment received is +X (Transfer).
- Transfer category is excluded from income/expense rollups (handled in spreadsheet).

Reads any *.csv under ./Transaction CSVs/ and writes ./Transactions.csv.
Format is auto-detected from the header row.

Run: python3 normalize.py
"""
import csv
import re
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
INPUT_DIR = BASE / "Transaction CSVs"
RULES_PATH = BASE / "Rules.csv"
OUTPUT_PATH = BASE / "Transactions.csv"


def load_user_rules() -> list[tuple[str, str]]:
    """Load user-defined keyword→category rules. Higher priority than built-ins."""
    if not RULES_PATH.exists():
        return []
    rules = []
    with RULES_PATH.open() as f:
        for r in csv.DictReader(f):
            kw = (r.get("Keyword") or "").strip()
            cat = (r.get("Category") or "").strip()
            if kw and cat:
                rules.append((kw.upper(), cat))
    return rules


USER_RULES: list[tuple[str, str]] = []  # populated in main()

# --- Category mappings ----------------------------------------------------

# For source files that already have a Category column (FirstView, Capital One)
CATEGORY_MAP = {
    "Income/Paycheck": "Salary",
    "Income": "Salary",
    "Uncategorized Income": "Other Income",
    "Food/Dining Out": "Dining Out",
    "Food": "Dining Out",
    "Food/Alcohol": "Dining Out",
    "Dining": "Dining Out",
    "Food/Groceries": "Groceries",
    "Transportation/Fuel": "Gas",
    "Gas/Automotive": "Gas",
    "Transportation": "Transportation",
    "Transportation/Maintenance": "Transportation",
    "Transportation/Other": "Transportation",
    "Shopping": "Shopping",
    "Clothing": "Shopping",
    "Housing/Supplies": "Shopping",
    "Housing/Furnishings": "Shopping",
    "Merchandise": "Shopping",
    "Housing": "Rent",
    "Medical": "Healthcare",
    "Healthcare - HSA Spending Account": "Healthcare",
    "Healthcare - HSA Spending Account/Copays": "Healthcare",
    "Healthcare - HSA Spending Account/Out of Pocket": "Healthcare",
    "Health Care": "Healthcare",
    "Insurance": "Insurance",
    "Entertainment": "Entertainment",
    "Utilities/Phone": "Phone",
    "Publications": "Subscriptions",
    "Pets": "Pets",
    "Pets/Other": "Pets",
    "Personal Care": "Personal Care",
    "Education": "Education",
    "Business": "Business",
    "Housing/Home Office": "Business",
    "Financial Services": "Fees & ATM",
    "Financial Services/ATM": "Fees & ATM",
    "Fee/Interest Charge": "Fees & ATM",
    "Gift": "Gifts & Donations",
    "Contributions": "Gifts & Donations",
    "Miscellaneous": "Uncategorized",
    "Uncategorized Expense": "Uncategorized",
    "Transfer Between Accounts/Credit Card Payment": "Transfer",
    "Transfer Between Accounts": "Transfer",
    "Payment/Credit": "Transfer",
}

# For files without categories (Robinhood) — exact merchant match (case-insensitive)
MERCHANT_CATEGORY = {
    "doordash": "Dining Out",
    "dunkin' donuts": "Dining Out",
    "taco felix": "Dining Out",
    "chick-fil-a": "Dining Out",
    "mcdonald's": "Dining Out",
    "mcdonald’s": "Dining Out",
    "scooter's coffee": "Dining Out",
    "moe's original bbq": "Dining Out",
    "whataburger": "Dining Out",
    "chipotle": "Dining Out",
    "downtown beer & tobacco": "Dining Out",
    "bp": "Gas",
    "nex fuel": "Gas",
    "desoto family wellness": "Healthcare",
    "baptist memorial hospital-desoto": "Healthcare",
    "apple": "Subscriptions",
    "steam games": "Entertainment",
    "amazon": "Shopping",
    "walmart": "Shopping",
    "kroger": "Groceries",
    "dollar general": "Shopping",
    "autozone": "Transportation",
    "cricket wireless": "Phone",
    "interest charge": "Fees & ATM",
    "payment": "Transfer",
    "nexcom hq": "Shopping",
    "20350 crunch austin peay": "Personal Care",  # gym
    "hernando self storage": "Rent",  # storage rental
    "sport clips": "Personal Care",
    "booksy": "Personal Care",
    "hollywood feed": "Pets",
    "lowe's": "Shopping",
    "zaxby's": "Dining Out",
    "silky o'sullivan's": "Dining Out",
    "cheers": "Dining Out",
    "arby's": "Dining Out",
    "amazon retail": "Shopping",
    "rei": "Shopping",
    "toyota": "Transportation",
    "caliber co": "Transportation",
    "dixie memorial": "Pets",
    "delta airlines": "Travel",
    "airbnb": "Travel",
    "coinbase inc.": "Investments",
    "whoop": "Subscriptions",
    "fedexforum": "Entertainment",
    "home depot": "Shopping",
    "elfo grisanti's": "Dining Out",
    "yakiniku": "Dining Out",
    "the velvet ditch spo": "Dining Out",
    "lucky dog": "Dining Out",
    "blind alley bayou her": "Dining Out",
    "rock auto": "Transportation",
    "rack room shoes": "Shopping",
    "honda": "Transportation",
    "state farm insurance": "Insurance",
    "many islands camp & canoe rental": "Travel",
    "crazy horse recreational park": "Entertainment",
    "hinton floral": "Gifts & Donations",
}

# Keyword fallback (matched against Merchant + Description)
KEYWORD_CATEGORY = [
    ("FUEL", "Gas"), ("GAS STATION", "Gas"), ("SHELL", "Gas"), ("EXXON", "Gas"),
    ("CHEVRON", "Gas"), ("RACETRAC", "Gas"), ("RACEWAY", "Gas"), ("MARATHON", "Gas"),
    ("PHARMACY", "Healthcare"), ("HOSPITAL", "Healthcare"), ("WELLNESS", "Healthcare"),
    ("DENTAL", "Healthcare"), ("CLINIC", "Healthcare"), ("MEDICAL", "Healthcare"),
    ("GROCERY", "Groceries"), ("WHOLE FOODS", "Groceries"), ("TRADER JOE", "Groceries"),
    ("ALDI", "Groceries"), ("PUBLIX", "Groceries"),
    ("RESTAURANT", "Dining Out"), ("COFFEE", "Dining Out"), ("PIZZA", "Dining Out"),
    ("CAFE", "Dining Out"), ("BBQ", "Dining Out"), ("STEAKHOUSE", "Dining Out"),
    ("TACO", "Dining Out"), ("BURGER", "Dining Out"), ("DELI", "Dining Out"),
    ("STARBUCKS", "Dining Out"), ("CHIPOTLE", "Dining Out"),
    ("PHONE", "Phone"), ("WIRELESS", "Phone"), ("VERIZON", "Phone"), ("T-MOBILE", "Phone"),
    ("NETFLIX", "Subscriptions"), ("SPOTIFY", "Subscriptions"), ("HULU", "Subscriptions"),
    ("DISNEY+", "Subscriptions"), ("APPLE.COM/BILL", "Subscriptions"),
    ("STEAM", "Entertainment"), ("XBOX", "Entertainment"), ("PLAYSTATION", "Entertainment"),
    ("TARGET", "Shopping"), ("COSTCO", "Shopping"), ("BEST BUY", "Shopping"),
    ("UBER", "Transportation"), ("LYFT", "Transportation"),
    ("PETCO", "Pets"), ("PETSMART", "Pets"), ("CHEWY", "Pets"),
    ("GYM", "Personal Care"), ("CRUNCH", "Personal Care"),
    ("USAA P&C", "Insurance"), ("USAA FSB IC", "Insurance"),
    ("CASH APP", "Transfer"), ("INTERBANK", "Transfer"), ("VENMO", "Transfer"), ("ZELLE", "Transfer"),
    ("UNIV OF MISSISSIPPI", "Education"), ("UNIVERSITY", "Education"),
    ("MMS-BAPTIST", "Healthcare"), ("BAPTIST DE", "Healthcare"),
    ("COINBASE", "Investments"),
    ("IRS USATAXPYMT", "Taxes"), ("USATAXPYMT", "Taxes"),
    ("AIRBNB", "Travel"), ("DELTA AIR", "Travel"), ("AIRLINES", "Travel"),
    ("HOTEL", "Travel"), ("MARRIOTT", "Travel"), ("HILTON", "Travel"),
    ("CALIBER COLLISI", "Transportation"), ("TOYOTA", "Transportation"),
    ("DIXIE MEMORIAL", "Pets"),
    ("WHOOP", "Subscriptions"),
    ("DRAFTKINGS", "Entertainment"), ("FEDEXFORUM", "Entertainment"),
    ("KRAKEN", "Investments"),
    ("STATE FARM", "Insurance"),
    ("HOME DEPOT", "Shopping"), ("WM SUPERCE", "Shopping"), ("RACK ROOM", "Shopping"),
    ("MS.GOV", "Taxes"),
    ("ELFO GRISANTI", "Dining Out"), ("YAKINIKU", "Dining Out"),
    ("VELVET DITCH", "Dining Out"), ("BLIND ALLEY", "Dining Out"),
    ("LUCKY DOG", "Dining Out"), ("PRIMOS A C", "Dining Out"),
    ("ROCK AUTO", "Transportation"), ("SOUTHAVEN HONDA", "Transportation"),
    ("DESOTO COUNTY JUSTICE", "Fees & ATM"),
    ("MANY ISLANDS CAMP", "Travel"),
    ("CRAZY HORSE RECREATIONAL", "Entertainment"),
    ("HINTON FLORAL", "Gifts & Donations"),
    ("HERNANDO SELF STORAGE", "Rent"),
    ("MARSHALLS", "Shopping"), ("H&M", "Shopping"), ("NIKE", "Shopping"),
    ("O'REILLY", "Transportation"),
    ("CARNIVAL", "Travel"),
    ("DOWNTOWN B", "Dining Out"), ("DOWNTOWN BEER", "Dining Out"),
    ("HOLLYWOOD FEED", "Pets"),
]


def clean_description(s: str) -> str:
    s = re.sub(r"^\d{4}(?=[A-Z])", "", s)
    s = re.sub(r"^POS\s+(DB|DC|PUR|CR)\s+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\*{4,}\d+\s*$", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def categorize_by_merchant(merchant: str, description: str) -> str:
    haystack = f"{merchant} {description}".upper()
    # User-defined rules from Rules.csv win over built-ins.
    for keyword, cat in USER_RULES:
        if keyword in haystack:
            return cat
    key = merchant.lower().strip()
    if key in MERCHANT_CATEGORY:
        return MERCHANT_CATEGORY[key]
    for keyword, cat in KEYWORD_CATEGORY:
        if keyword in haystack:
            return cat
    return "Uncategorized"


def map_category(src: str) -> str:
    return CATEGORY_MAP.get(src, "Uncategorized")


# --- Per-format handlers --------------------------------------------------

def normalize_firstview(path: Path) -> list[dict]:
    out = []
    with path.open() as f:
        for r in csv.DictReader(f):
            date = datetime.strptime(r["Date"], "%m/%d/%Y").strftime("%Y-%m-%d")
            d = r["Debit"].strip()
            c = r["Credit"].strip()
            amount = float(d) if d else float(c)  # debits already negative in this file
            desc = clean_description(r["Description"])
            category = map_category(r["Category"])
            if category == "Uncategorized":
                category = categorize_by_merchant("", desc)
            out.append({
                "Date": date,
                "Description": desc,
                "Amount": f"{amount:.2f}",
                "Account": "FirstView Checking",
                "Category": category,
                "Notes": "",
            })
    return out


def normalize_capital_one(path: Path) -> list[dict]:
    out = []
    with path.open() as f:
        for r in csv.DictReader(f):
            d = r["Debit"].strip()
            c = r["Credit"].strip()
            amount = -abs(float(d)) if d else abs(float(c))
            desc = clean_description(r["Description"])
            category = map_category(r["Category"])
            if category == "Uncategorized":
                category = categorize_by_merchant("", desc)
            out.append({
                "Date": r["Transaction Date"],
                "Description": desc,
                "Amount": f"{amount:.2f}",
                "Account": f"Capital One {r['Card No.'].strip()}",
                "Category": category,
                "Notes": "",
            })
    return out


def normalize_robinhood(path: Path) -> list[dict]:
    """Robinhood Card: Amount positive = charge, negative = payment/refund.
    No Category column — categorize from Merchant/Description.
    Filter out Declined; keep Pending."""
    out = []
    with path.open() as f:
        for r in csv.DictReader(f):
            if r["Status"] == "Declined":
                continue
            raw_amount = float(r["Amount"])
            # Type drives category for non-purchase rows
            t = r["Type"]
            if t == "Payment":
                category = "Transfer"
            elif t in ("Interest", "Fee"):
                category = "Fees & ATM"
            else:  # Purchase, Refund
                category = categorize_by_merchant(r["Merchant"], r["Description"])
            # Flip sign: file's positive (charge) = user's negative (outflow)
            amount = -raw_amount
            cardholder = r["Cardholder"].split()[0] if r["Cardholder"] else ""
            out.append({
                "Date": r["Date"],
                "Description": clean_description(f"{r['Merchant']} - {r['Description']}".strip(" -")),
                "Amount": f"{amount:.2f}",
                "Account": "Robinhood Credit Card",
                "Category": category,
                "Notes": cardholder,
            })
    return out


HANDLERS = {
    # match by frozenset of column headers
    frozenset(["Date", "Account", "Description", "Check #", "Category", "Credit", "Debit"]): normalize_firstview,
    frozenset(["Transaction Date", "Posted Date", "Card No.", "Description", "Category", "Debit", "Credit"]): normalize_capital_one,
    frozenset(["Date", "Time", "Cardholder", "Amount", "Points", "Balance", "Status", "Type", "Merchant", "Description"]): normalize_robinhood,
}


def detect_and_normalize(path: Path) -> list[dict]:
    with path.open() as f:
        header = next(csv.reader(f))
    fp = frozenset(header)
    handler = HANDLERS.get(fp)
    if not handler:
        raise ValueError(f"Unknown format for {path.name}: columns = {header}")
    return handler(path)


def merge_with_existing(new_rows: list[dict]) -> tuple[list[dict], int]:
    """Preserve any Category/Notes edits the user has already made in Transactions.csv.

    Matches new rows to existing rows on (Date, Description, Amount, Account).
    Returns (merged_rows, preserved_count).
    """
    if not OUTPUT_PATH.exists():
        return new_rows, 0

    from collections import defaultdict
    existing_by_key: dict[tuple, list[tuple[str, str]]] = defaultdict(list)
    with OUTPUT_PATH.open() as f:
        for r in csv.DictReader(f):
            key = (r["Date"], r["Description"], r["Amount"], r["Account"])
            existing_by_key[key].append((r.get("Category", ""), r.get("Notes", "") or ""))

    preserved = 0
    out = []
    for row in new_rows:
        key = (row["Date"], row["Description"], row["Amount"], row["Account"])
        if existing_by_key.get(key):
            old_cat, old_notes = existing_by_key[key].pop(0)
            if old_cat:
                row = dict(row)
                row["Category"] = old_cat
                if old_notes and not row.get("Notes"):
                    row["Notes"] = old_notes
                preserved += 1
        out.append(row)
    return out, preserved


def main():
    global USER_RULES
    USER_RULES = load_user_rules()
    print(f"Loaded {len(USER_RULES)} user rules from Rules.csv")
    print()

    all_rows = []
    for csv_path in sorted(INPUT_DIR.glob("*.csv")):
        rows = detect_and_normalize(csv_path)
        print(f"  {len(rows):5d} rows from {csv_path.name}")
        all_rows.extend(rows)

    all_rows.sort(key=lambda r: r["Date"])

    # Preserve any manual categorization edits already in Transactions.csv
    all_rows, preserved = merge_with_existing(all_rows)
    if preserved:
        print(f"\nPreserved {preserved} existing categorizations from Transactions.csv")

    with OUTPUT_PATH.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["Date", "Description", "Amount", "Account", "Category", "Notes"])
        w.writeheader()
        w.writerows(all_rows)

    from collections import Counter
    print()
    print(f"Wrote {len(all_rows)} rows to {OUTPUT_PATH.name}")
    print(f"Date range: {all_rows[0]['Date']} to {all_rows[-1]['Date']}")
    print()
    print("By Account:")
    for a, n in Counter(r["Account"] for r in all_rows).most_common():
        print(f"  {n:5d}  {a}")
    print()
    print("By Category:")
    for c, n in Counter(r["Category"] for r in all_rows).most_common():
        print(f"  {n:5d}  {c}")


if __name__ == "__main__":
    main()
