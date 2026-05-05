"""
Export MongoDB Atlas data to Excel for Power BI Desktop.

Usage (run locally, not inside Docker):
    pip install pymongo[srv] pandas openpyxl dnspython
    python export_for_powerbi.py

Power BI: File → Get Data → Excel Workbook → select globalretail_export.xlsx
          Load sheets: Transactions, Monthly_Revenue, Customer_Summary
"""

import os
import sys
from datetime import datetime

import pandas as pd
from pymongo import MongoClient

MONGO_URI   = os.environ.get("MONGO_URI") or input("Paste your MONGO_URI: ").strip()
DB_NAME     = "globalretail"
COLLECTION  = "transactions"
OUTPUT_FILE = "globalretail_export.xlsx"


def main():
    print(f"[{datetime.now():%H:%M:%S}] Connecting to MongoDB Atlas…")
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10_000)
        client.admin.command("ping")
    except Exception as exc:
        sys.exit(f"Connection failed: {exc}")

    col = client[DB_NAME][COLLECTION]
    total = col.count_documents({})
    print(f"[{datetime.now():%H:%M:%S}] Collection has {total:,} records. Fetching…")

    if total == 0:
        sys.exit("No data found. Run the ETL pipeline first (Airflow UI → Trigger DAG).")

    df = pd.DataFrame(list(col.find({}, {"_id": 0})))
    client.close()
    print(f"[{datetime.now():%H:%M:%S}] Fetched {len(df):,} rows.")

    # ── Derived columns useful for Power BI ──────────────────────────────
    df["invoice_date"]       = pd.to_datetime(df["invoice_date"])
    df["year_month"]         = df["invoice_date"].dt.to_period("M").astype(str)
    df["year"]               = df["invoice_date"].dt.year
    df["month"]              = df["invoice_date"].dt.month
    df["month_name"]         = df["invoice_date"].dt.strftime("%b")

    # ── Sheet 2 — Monthly Revenue by Region ──────────────────────────────
    monthly = (
        df.groupby(["region", "year_month", "year", "month"])
        .agg(
            revenue_eur   = ("revenue_eur",  "sum"),
            total_orders  = ("invoice_no",   "nunique"),
            total_units   = ("quantity",     "sum"),
        )
        .reset_index()
        .sort_values(["region", "year_month"])
    )

    # ── Sheet 3 — Customer Summary (Scatter: Orders vs Revenue) ──────────
    customers = (
        df.groupby("customer_id")
        .agg(
            total_revenue_eur = ("revenue_eur",  "sum"),
            total_orders      = ("invoice_no",   "nunique"),
            total_items       = ("quantity",     "sum"),
            country           = ("country",      "first"),
            region            = ("region",       "first"),
            first_purchase    = ("invoice_date", "min"),
            last_purchase     = ("invoice_date", "max"),
        )
        .reset_index()
        .sort_values("total_revenue_eur", ascending=False)
    )

    # ── Write Excel workbook ──────────────────────────────────────────────
    print(f"[{datetime.now():%H:%M:%S}] Writing {OUTPUT_FILE}…")
    with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:
        df.to_excel(writer,     sheet_name="Transactions",     index=False)
        monthly.to_excel(writer, sheet_name="Monthly_Revenue", index=False)
        customers.to_excel(writer, sheet_name="Customer_Summary", index=False)

    print(f"[{datetime.now():%H:%M:%S}] Done → {OUTPUT_FILE}")
    print()
    print("Power BI steps:")
    print("  1. Open Power BI Desktop")
    print("  2. Home → Get Data → Excel Workbook → select globalretail_export.xlsx")
    print("  3. Load all three sheets (Transactions, Monthly_Revenue, Customer_Summary)")
    print("  4. Build visuals:")
    print("     - Bar chart  : Monthly_Revenue[region] vs [revenue_eur]")
    print("     - Line chart : Monthly_Revenue[year_month] vs [revenue_eur] (legend: region)")
    print("     - Scatter    : Customer_Summary  X=[total_orders]  Y=[total_revenue_eur]  Size=[total_items]")


if __name__ == "__main__":
    main()
