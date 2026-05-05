"""
GlobalRetail Corp — ETL Pipeline
Extract (CSV + APIs) → Transform → Load (MongoDB Atlas)

DAG: globalretail_etl_pipeline
Schedule: daily  (@daily)
"""

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from pymongo import MongoClient, ASCENDING, UpdateOne
from pymongo.errors import BulkWriteError

from airflow import DAG
from airflow.operators.python import PythonOperator

# ─── Configuration ─────────────────────────────────────────────────────────────
MONGO_URI        = os.environ.get("MONGO_URI")
DB_NAME          = "globalretail"
COLLECTION_NAME  = "transactions"
META_COLLECTION  = "pipeline_metadata"

DATA_DIR  = Path("/opt/airflow/data")
TMP_DIR   = DATA_DIR / "tmp"
CSV_PATH  = DATA_DIR / "online_retail.csv"

BATCH_SIZE       = 1000
COUNTRIES_API    = "https://restcountries.com/v3.1/all?fields=name,region,subregion,population"
FX_API           = "https://api.frankfurter.dev/v1/latest"
REQUEST_TIMEOUT  = 30
MAX_RETRIES      = 3

# Known mismatches between Online Retail dataset country names and REST Countries API
COUNTRY_ALIASES = {
    "eire":                "ireland",
    "channel islands":     "jersey",
    "rsa":                 "south africa",
    "usa":                 "united states",
    "czech republic":      "czechia",
    "european community":  None,
    "unspecified":         None,
    "west indies":         None,
}

logger = logging.getLogger(__name__)

# ─── Shared helpers ─────────────────────────────────────────────────────────────

def _mongo_client() -> MongoClient:
    if not MONGO_URI:
        raise EnvironmentError("MONGO_URI is not set. Check your .env file.")
    return MongoClient(MONGO_URI, serverSelectionTimeoutMS=10_000)


def _fetch_with_retry(url: str, params: dict = None) -> dict:
    """GET a JSON endpoint with exponential-backoff retries."""
    for attempt in range(MAX_RETRIES):
        try:
            logger.info("GET %s  (attempt %d/%d)", url, attempt + 1, MAX_RETRIES)
            r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            wait = 2 ** attempt
            if attempt == MAX_RETRIES - 1:
                logger.error("All %d attempts failed for %s: %s", MAX_RETRIES, url, exc)
                raise
            logger.warning("Request error (%s). Retrying in %ds…", exc, wait)
            time.sleep(wait)


def _record_id(invoice_no: str, stock_code: str, invoice_date, qty, price) -> str:
    """SHA-256 fingerprint of the five fields that uniquely identify a line item."""
    key = f"{invoice_no}|{stock_code}|{invoice_date}|{int(qty)}|{price:.4f}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


# ─── Task 1 — Extract ──────────────────────────────────────────────────────────

def extract_data(**context):
    """
    - Reads the last-processed watermark from MongoDB.
    - Loads the CSV and retains only rows newer than the watermark.
    - Calls REST Countries API and Frankfurter FX API.
    - Persists raw artefacts to /opt/airflow/data/tmp/ for the next task.
    """
    t0 = time.time()
    logger.info("=" * 64)
    logger.info("EXTRACT  started at %s", datetime.utcnow().isoformat())
    logger.info("=" * 64)

    TMP_DIR.mkdir(parents=True, exist_ok=True)

    # ── 1.  Retrieve watermark ─────────────────────────────────────────────
    try:
        client = _mongo_client()
        wm_doc = client[DB_NAME][META_COLLECTION].find_one({"_id": "watermark"})
        client.close()
    except Exception as exc:
        logger.error("MongoDB connection failed: %s", exc)
        raise

    if wm_doc and "last_processed_date" in wm_doc:
        watermark = wm_doc["last_processed_date"].replace(tzinfo=None)
        logger.info("Watermark found: %s", watermark)
    else:
        watermark = datetime(2000, 1, 1)
        logger.info("No watermark — full load.")

    # ── 2.  Read CSV (incremental filter) ─────────────────────────────────
    if not CSV_PATH.exists():
        raise FileNotFoundError(
            f"Dataset not found: {CSV_PATH}\n"
            "Download 'Online Retail.xlsx' from Kaggle, save as online_retail.csv in ./data/"
        )

    logger.info("Reading %s …", CSV_PATH)
    df = pd.read_csv(
        CSV_PATH,
        encoding="latin-1",
        dtype=str,
    )
    # Normalise column names — Online Retail II uses different names than v1
    df = df.rename(columns={
        "Invoice":     "InvoiceNo",
        "Price":       "UnitPrice",
        "Customer ID": "CustomerID",
    })
    total_csv = len(df)
    logger.info("CSV rows total: %d | Columns: %s", total_csv, list(df.columns))

    df["InvoiceDate"] = pd.to_datetime(df["InvoiceDate"], errors="coerce")
    df = df.dropna(subset=["InvoiceDate"])
    df_new = df[df["InvoiceDate"] > watermark].copy()
    logger.info("Rows after watermark filter (>%s): %d", watermark, len(df_new))

    df_new.to_pickle(str(TMP_DIR / "raw_data.pkl"))

    # ── 3.  REST Countries API ─────────────────────────────────────────────
    logger.info("Fetching REST Countries API…")
    try:
        raw_countries = _fetch_with_retry(COUNTRIES_API)
    except Exception as exc:
        logger.error("REST Countries API failed — aborting extract: %s", exc)
        raise

    countries_map: dict = {}
    for c in raw_countries:
        name = c.get("name", {}).get("common", "").lower().strip()
        if name:
            countries_map[name] = {
                "region":     c.get("region", "Unknown"),
                "sub_region": c.get("subregion", ""),
                "population": c.get("population", 0),
            }

    for alias, canonical in COUNTRY_ALIASES.items():
        if canonical and canonical in countries_map:
            countries_map[alias] = countries_map[canonical]

    logger.info("Countries mapped: %d", len(countries_map))
    with open(TMP_DIR / "countries_map.json", "w") as fh:
        json.dump(countries_map, fh)

    # ── 4.  Frankfurter FX API (GBP → EUR) ────────────────────────────────
    logger.info("Fetching GBP → EUR exchange rate…")
    try:
        fx = _fetch_with_retry(FX_API, params={"base": "GBP", "symbols": "EUR"})
    except Exception as exc:
        logger.error("Frankfurter API failed — aborting extract: %s", exc)
        raise

    gbp_to_eur = fx["rates"]["EUR"]
    logger.info("GBP → EUR: %.6f  (date: %s)", gbp_to_eur, fx.get("date"))

    with open(TMP_DIR / "fx_rate.json", "w") as fh:
        json.dump({
            "gbp_to_eur":  gbp_to_eur,
            "rate_date":   fx.get("date", "unknown"),
            "fetched_at":  datetime.utcnow().isoformat(),
        }, fh)

    elapsed = time.time() - t0
    logger.info(
        "EXTRACT  finished in %.1fs | extracted=%d / total_csv=%d",
        elapsed, len(df_new), total_csv,
    )
    context["ti"].xcom_push(key="records_extracted", value=len(df_new))
    context["ti"].xcom_push(key="watermark_used",    value=watermark.isoformat())


# ─── Task 2 — Transform ────────────────────────────────────────────────────────

def transform_data(**context):
    """
    - Cleans: drops null CustomerID, negative Quantity, zero UnitPrice.
    - Normalises: lowercase + strip on Country, Description.
    - Converts: RevenueEUR = Quantity * UnitPrice * gbp_to_eur.
    - Enriches: Region, SubRegion, Population from the countries map.
    - Generates a deterministic SHA-256 _id for idempotency.
    """
    t0 = time.time()
    logger.info("=" * 64)
    logger.info("TRANSFORM started at %s", datetime.utcnow().isoformat())
    logger.info("=" * 64)

    df = pd.read_pickle(str(TMP_DIR / "raw_data.pkl"))
    with open(TMP_DIR / "countries_map.json") as fh:
        countries_map = json.load(fh)
    with open(TMP_DIR / "fx_rate.json") as fh:
        fx = json.load(fh)

    gbp_to_eur = fx["gbp_to_eur"]
    n_raw = len(df)
    logger.info("Records received for transformation: %d", n_raw)

    if n_raw == 0:
        logger.info("Nothing to transform — saving empty frame.")
        df.to_pickle(str(TMP_DIR / "transformed_data.pkl"))
        context["ti"].xcom_push(key="records_transformed", value=0)
        context["ti"].xcom_push(key="max_invoice_date",     value=None)
        return

    # ── Step 1 — Type coercion ─────────────────────────────────────────────
    df["Quantity"]   = pd.to_numeric(df["Quantity"],   errors="coerce")
    df["UnitPrice"]  = pd.to_numeric(df["UnitPrice"],  errors="coerce")
    df["CustomerID"] = df["CustomerID"].astype(str).str.strip()

    # ── Step 2 — Cleaning ──────────────────────────────────────────────────
    df = df.dropna(subset=["Quantity", "UnitPrice", "InvoiceDate"])
    df = df[~df["CustomerID"].str.lower().isin(["nan", "none", ""])]
    df = df[df["Quantity"] > 0]
    df = df[df["UnitPrice"] > 0]
    n_clean = len(df)
    logger.info("After cleaning: %d rows (dropped %d)", n_clean, n_raw - n_clean)

    if n_clean == 0:
        logger.info("All rows removed during cleaning.")
        df.to_pickle(str(TMP_DIR / "transformed_data.pkl"))
        context["ti"].xcom_push(key="records_transformed", value=0)
        context["ti"].xcom_push(key="max_invoice_date",     value=None)
        return

    # ── Step 3 — Normalisation ─────────────────────────────────────────────
    df["Country"]     = df["Country"].str.lower().str.strip()
    df["Description"] = df["Description"].fillna("unknown").str.lower().str.strip()
    df["InvoiceNo"]   = df["InvoiceNo"].str.strip()
    df["StockCode"]   = df["StockCode"].str.strip()
    df["CustomerID"]  = df["CustomerID"].str.strip()

    # Resolve known aliases before the map lookup
    df["Country"] = df["Country"].replace(
        {alias: canon for alias, canon in COUNTRY_ALIASES.items() if canon}
    )

    # ── Step 4 — Currency conversion ──────────────────────────────────────
    df["revenue_gbp"]      = (df["Quantity"] * df["UnitPrice"]).round(4)
    df["revenue_eur"]      = (df["revenue_gbp"] * gbp_to_eur).round(4)
    df["fx_rate_gbp_eur"]  = round(gbp_to_eur, 6)
    df["fx_rate_date"]     = fx.get("rate_date", "")

    # ── Step 5 — Geopolitical enrichment ──────────────────────────────────
    def _lookup(country: str, field: str, default):
        return countries_map.get(country, {}).get(field, default)

    df["region"]     = df["Country"].map(lambda c: _lookup(c, "region",     "Unknown"))
    df["sub_region"] = df["Country"].map(lambda c: _lookup(c, "sub_region", ""))
    df["population"] = df["Country"].map(lambda c: _lookup(c, "population", 0))

    unmatched = df[df["region"] == "Unknown"]["Country"].unique()
    if len(unmatched):
        logger.warning("Unmatched countries (%d): %s", len(unmatched), list(unmatched)[:15])

    # ── Step 6 — Idempotent record ID ─────────────────────────────────────
    df["_id"] = df.apply(
        lambda r: _record_id(
            r["InvoiceNo"], r["StockCode"],
            r["InvoiceDate"], r["Quantity"], r["UnitPrice"],
        ),
        axis=1,
    )

    # ── Step 7 — Rename & select final columns ────────────────────────────
    df = df.rename(columns={
        "InvoiceNo":   "invoice_no",
        "StockCode":   "stock_code",
        "Description": "description",
        "Quantity":    "quantity",
        "InvoiceDate": "invoice_date",
        "UnitPrice":   "unit_price",
        "CustomerID":  "customer_id",
        "Country":     "country",
    })

    FINAL_COLS = [
        "_id", "invoice_no", "stock_code", "description",
        "quantity", "invoice_date", "unit_price", "customer_id",
        "country", "region", "sub_region", "population",
        "revenue_gbp", "revenue_eur", "fx_rate_gbp_eur", "fx_rate_date",
    ]
    df = df[FINAL_COLS]

    df.to_pickle(str(TMP_DIR / "transformed_data.pkl"))

    max_date = df["invoice_date"].max()
    elapsed  = time.time() - t0
    logger.info(
        "TRANSFORM finished in %.1fs | ready=%d | max_date=%s",
        elapsed, len(df), max_date,
    )
    context["ti"].xcom_push(key="records_transformed", value=len(df))
    context["ti"].xcom_push(
        key="max_invoice_date",
        value=max_date.isoformat() if pd.notna(max_date) else None,
    )


# ─── Task 3 — Load ─────────────────────────────────────────────────────────────

def load_to_mongo(**context):
    """
    - Creates query-supporting indexes if absent.
    - Batch-upserts records using UpdateOne($setOnInsert) for idempotency:
        - New records   → inserted.
        - Existing _ids → matched but NOT overwritten (safe re-runs).
    - Updates the watermark in pipeline_metadata after a successful load.
    """
    t0 = time.time()
    logger.info("=" * 64)
    logger.info("LOAD     started at %s", datetime.utcnow().isoformat())
    logger.info("=" * 64)

    df = pd.read_pickle(str(TMP_DIR / "transformed_data.pkl"))
    n  = len(df)
    logger.info("Records to load: %d", n)

    if n == 0:
        logger.info("No new records — pipeline complete (idempotent no-op).")
        return

    # ── Convert DataFrame to plain Python dicts ───────────────────────────
    # Note: itertuples() renames columns starting with '_' (like _id), so we use to_dict()
    records = df.to_dict(orient="records")
    for rec in records:
        # pandas Timestamp → Python datetime (PyMongo requires datetime.datetime)
        if isinstance(rec.get("invoice_date"), pd.Timestamp):
            rec["invoice_date"] = rec["invoice_date"].to_pydatetime().replace(tzinfo=None)
        # numpy scalars → Python natives
        for k, v in list(rec.items()):
            if isinstance(v, np.integer):
                rec[k] = int(v)
            elif isinstance(v, np.floating):
                rec[k] = None if np.isnan(v) else float(v)

    # ── Connect & ensure indexes ──────────────────────────────────────────
    try:
        client     = _mongo_client()
        db         = client[DB_NAME]
        col        = db[COLLECTION_NAME]
        meta_col   = db[META_COLLECTION]

        col.create_index([("invoice_date",  ASCENDING)], background=True)
        col.create_index([("customer_id",   ASCENDING), ("invoice_date", ASCENDING)], background=True)
        col.create_index([("region",        ASCENDING)], background=True)
        col.create_index([("country",       ASCENDING)], background=True)
        logger.info("Indexes ensured.")
    except Exception as exc:
        logger.error("MongoDB setup failed: %s", exc)
        raise

    # ── Batch upsert ──────────────────────────────────────────────────────
    total_inserted = 0
    total_skipped  = 0
    num_batches    = (n + BATCH_SIZE - 1) // BATCH_SIZE

    for batch_idx, start in enumerate(range(0, n, BATCH_SIZE), 1):
        batch = records[start : start + BATCH_SIZE]
        ops   = [
            UpdateOne(
                filter={"_id": rec["_id"]},
                update={"$setOnInsert": rec},
                upsert=True,
            )
            for rec in batch
        ]
        try:
            res = col.bulk_write(ops, ordered=False)
            total_inserted += res.upserted_count
            total_skipped  += res.matched_count
            logger.info(
                "Batch %d/%d → inserted=%d  skipped(dup)=%d",
                batch_idx, num_batches, res.upserted_count, res.matched_count,
            )
        except BulkWriteError as bwe:
            n_ins = bwe.details.get("nInserted", 0)
            n_err = len(bwe.details.get("writeErrors", []))
            logger.warning(
                "Batch %d BulkWriteError — inserted=%d  errors=%d",
                batch_idx, n_ins, n_err,
            )
            total_inserted += n_ins

    logger.info("Bulk load done: inserted=%d  skipped=%d", total_inserted, total_skipped)

    # ── Update watermark ──────────────────────────────────────────────────
    max_date_iso = context["ti"].xcom_pull(task_ids="transform_data", key="max_invoice_date")
    if max_date_iso:
        try:
            max_date = datetime.fromisoformat(max_date_iso).replace(tzinfo=None)
            meta_col.update_one(
                {"_id": "watermark"},
                {"$set": {
                    "last_processed_date": max_date,
                    "updated_at":          datetime.utcnow(),
                    "last_run_inserted":   total_inserted,
                    "last_run_skipped":    total_skipped,
                }},
                upsert=True,
            )
            logger.info("Watermark updated → %s", max_date)
        except Exception as exc:
            logger.error("Watermark update failed (non-fatal): %s", exc)

    client.close()

    elapsed = time.time() - t0
    logger.info(
        "LOAD     finished in %.1fs | inserted=%d | skipped=%d",
        elapsed, total_inserted, total_skipped,
    )


# ─── DAG Definition ────────────────────────────────────────────────────────────

default_args = {
    "owner":            "data-engineering",
    "depends_on_past":  False,
    "email_on_failure": False,
    "email_on_retry":   False,
    "retries":          1,
    "retry_delay":      timedelta(minutes=5),
}

with DAG(
    dag_id="globalretail_etl_pipeline",
    description="GlobalRetail Corp — CSV + API → Transform → MongoDB Atlas",
    default_args=default_args,
    schedule_interval="@daily",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    max_active_runs=1,
    tags=["etl", "globalretail", "mongodb", "capstone"],
    doc_md="""
## GlobalRetail ETL Pipeline

Incrementally ingests Online Retail transaction data, enriches each record
with geopolitical metadata (region, population) and live GBP→EUR FX rates,
then upserts the result to MongoDB Atlas for BI reporting.

| Task | Responsibility |
|------|---------------|
| `extract_data`  | Watermark read · CSV filter · API calls |
| `transform_data`| Clean · Normalise · Enrich · Convert |
| `load_to_mongo` | Index setup · Batch upsert · Watermark write |
    """,
) as dag:

    extract_task = PythonOperator(
        task_id="extract_data",
        python_callable=extract_data,
    )

    transform_task = PythonOperator(
        task_id="transform_data",
        python_callable=transform_data,
    )

    load_task = PythonOperator(
        task_id="load_to_mongo",
        python_callable=load_to_mongo,
    )

    extract_task >> transform_task >> load_task
