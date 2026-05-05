# Technical Report — GlobalRetail Corp ETL Pipeline

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  Apache Airflow (Docker — LocalExecutor)                        │
│                                                                  │
│  extract_data ──► transform_data ──► load_to_mongo             │
└──────────┬──────────────────────────────────┬───────────────────┘
           │                                  │
    ┌──────▼──────┐                  ┌────────▼────────┐
    │  Sources     │                  │  MongoDB Atlas  │
    │  ─────────── │                  │  (M0 Free Tier) │
    │  CSV file    │                  │  ─────────────  │
    │  REST Ctries │                  │  transactions   │
    │  Frankfurter │                  │  pipeline_meta  │
    └─────────────┘                  └─────────────────┘
```

### Technology choices

| Component | Choice | Reason |
|-----------|--------|--------|
| Orchestration | Apache Airflow 2.9 (Docker, LocalExecutor) | Replaces cron; DAG dependencies, retries, UI |
| Data warehouse | MongoDB Atlas M0 | Schema-flexible, free tier, cloud-hosted |
| Transform layer | pandas 2.x | Rich ETL primitives, vectorised ops |
| Intermediate storage | Pickle files on shared volume | Avoids XCom size limits for large frames |

---

## 2. Pipeline Design Decisions

### 2.1 Incremental Extraction (Watermarking)

A document `{"_id": "watermark", "last_processed_date": <datetime>}` is
stored in the `pipeline_metadata` collection. On each run, `extract_data`
reads this timestamp and filters the CSV to only rows where
`InvoiceDate > watermark`. After a successful load, `load_to_mongo` advances
the watermark to the maximum `InvoiceDate` seen in that run.

**Effect:** The first run processes all ~500 k rows. Subsequent daily runs
process only the day's new transactions — typically a few hundred rows.

### 2.2 Idempotency

Each record is assigned a deterministic 32-character SHA-256 `_id` derived from:

```
SHA256( invoice_no | stock_code | invoice_date | quantity | unit_price )
```

Loading uses `UpdateOne($setOnInsert, upsert=True)`. If a record with that
`_id` already exists, the update matches but **does not overwrite** it.
Re-running the pipeline on the same data produces zero duplicates and zero
data changes.

### 2.3 Batch Loading

`insert_many` / `bulk_write` in chunks of 1 000 rows reduces round-trips and
keeps memory pressure low. On the M0 free tier (~500 MB RAM), loading 541 k
rows in one call would exhaust memory and timeout; batching avoids this.

---

## 3. Data Quality Challenges

### 3.1 Missing CustomerID

~135 k rows (~25 %) have no CustomerID. These are guest/anonymous
transactions with no BI value for customer analysis. They are dropped in the
cleaning step.

### 3.2 Negative Quantities / Zero Prices

Negative quantities represent **credit notes** (returns). Zero unit prices
represent samples or manual adjustments. Both are removed because they would
distort revenue figures. Separate return-analysis pipelines could process them
later.

### 3.3 Country Name Mismatches

The Online Retail dataset uses legacy country names ("EIRE", "RSA",
"European Community") that the REST Countries API does not recognise. A
`COUNTRY_ALIASES` dict maps known outliers to their canonical API names
before the enrichment step. Unmatched countries still load but receive
`region = "Unknown"` — logged as a warning so analysts can investigate.

### 3.4 CSV Encoding

The file uses **Latin-1** (ISO-8859-1) encoding, not UTF-8. Passing
`encoding="latin-1"` to `pd.read_csv` resolves garbled characters in product
descriptions.

---

## 4. Observability

- Every task logs `=== TASK START ===` / `=== TASK END ===` with wall-clock
  time so Airflow's log viewer shows per-task duration.
- Record counts are logged at each cleaning/filter step for lineage
  traceability.
- XCom keys (`records_extracted`, `records_transformed`, `max_invoice_date`)
  expose run metadata visible in the Airflow UI.
- API retries use exponential back-off (1 s, 2 s, 4 s) and log every attempt.

---

## 5. Error Handling Strategy

| Failure point | Behaviour |
|---------------|-----------|
| MongoDB unreachable | Raises immediately — Airflow retries the task |
| REST Countries API down | Retries 3×; raises if all fail — DAG run fails safely |
| Frankfurter API down | Same retry policy — without a valid FX rate, revenue figures would be wrong |
| BulkWriteError (partial batch) | Logs per-batch error, continues remaining batches |
| Watermark update fails | Logs as non-fatal warning — next run reprocesses the same window |

---

## 6. Power BI Dashboard

### Option A — Export script (works with M0 free tier)

```bash
# In the project root (not inside Docker)
pip install pymongo[srv] pandas openpyxl dnspython
set MONGO_URI=<your connection string>
python export_for_powerbi.py
```

Open `globalretail_export.xlsx` in Power BI Desktop via:
**Home → Get Data → Excel Workbook**

### Option B — MongoDB Atlas Charts (no download needed)

1. In Atlas, click your cluster → **Charts**
2. Connect to `globalretail.transactions`
3. Drag-and-drop to build the three required charts

### Dashboard visuals

| Chart | Type | Fields |
|-------|------|--------|
| Total Revenue by Region | Clustered Bar | `region` (axis), `revenue_eur` (value, Sum) |
| Revenue Trend by Month | Line | `year_month` (axis), `revenue_eur` (value), `region` (legend) |
| Customer Value Analysis | Scatter | X=`total_orders`, Y=`total_revenue_eur`, Size=`total_items` |

---

## 7. How to Run

```bash
# 1. Edit .env — add your MONGO_URI
# 2. Place online_retail.csv in ./data/

# 3. Start the stack
docker compose up airflow-init   # one-time DB migration + admin user
docker compose up -d             # webserver + scheduler

# 4. Open http://localhost:8080  (user: airflow / pass: airflow)
#    Activate the DAG: globalretail_etl_pipeline
#    Trigger manually or wait for the @daily schedule

# 5. Export for Power BI once data is loaded
python export_for_powerbi.py
```
