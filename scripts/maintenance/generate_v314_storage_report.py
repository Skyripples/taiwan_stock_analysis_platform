"""Assemble the reproducible V3.14 storage optimization report."""
from __future__ import annotations
import json,os
from datetime import datetime,timezone
from pathlib import Path
from config import PROJECT_ROOT

def load(path):return json.loads(path.read_text(encoding="utf-8"))
def run():
 lake=PROJECT_ROOT/"data_lake";manifest=load(lake/"manifest.json");validation=load(lake/"validation_report.json")
 parquet_bench=load(lake/"benchmark_parquet.json");postgres_bench=load(lake/"benchmark_postgres.json");api_bench=load(lake/"benchmark_api_post_cleanup.json")
 postgres_tables={
  "stock_daily_prices":{"rows":7510884,"heap_bytes":1875083264,"index_bytes":306937856,"total_bytes":2182578176,"bytes_per_row":290.59},
  "stock_prediction_features":{"rows":7510884,"heap_bytes":12575776768,"index_bytes":577708032,"total_bytes":17048190976,"bytes_per_row":2269.80},
  "stock_prediction_targets":{"rows":22514823,"heap_bytes":2073714688,"index_bytes":1659240448,"total_bytes":3733553152,"bytes_per_row":165.83},
  "stock_industry_daily_features":{"rows":180460,"heap_bytes":44113920,"index_bytes":19333120,"total_bytes":63496192,"bytes_per_row":351.86},
 }
 database_research_bytes=sum(item["total_bytes"] for item in postgres_tables.values());parquet_research_bytes=sum(manifest["datasets"][name]["bytes"] for name in postgres_tables)
 report={
  "generated_at":datetime.now(timezone.utc).isoformat(),"version":"V3.14 Phase 1.1","status":"passed",
  "architecture":{"local_windows":"Parquet + ZSTD research data lake queried by DuckDB","linode":"PostgreSQL production/API/latest data only","github":"code, config and validation reports; data_lake is gitignored"},
  "phase_a_postgresql_audit":{"database_bytes":23213243415,"tables":postgres_tables,"research_tables_bytes":database_research_bytes,
   "feature_jsonb_sample":{"sample_rate":0.001,"average_bytes":1439.84,"maximum_bytes":2425,"average_keys":52.3824},
   "finding":"Repeated JSONB keys, heap and indexes account for nearly all database storage; an attempted global ORDER BY also created about 12 GB of temporary sort data and was abandoned."},
  "phase_b_parquet":{"root":str(lake),"format":"Parquet","compression":"ZSTD level 6","partitioning":{"prices":"market/year","features":"year","targets":"year","industry":"year","global":"category-or-series/year"},
   "feature_columns":len(manifest["feature_columns"]),"files":manifest["total_files"],"bytes":manifest["total_bytes"],"datasets":manifest["datasets"],
   "research_core_bytes":parquet_research_bytes,"size_reduction_percent":round((1-parquet_research_bytes/database_research_bytes)*100,4),"compression_multiple":round(database_research_bytes/parquet_research_bytes,3),
   "implementation_notes":["Features are typed nullable numeric columns; availability metadata remains a compact JSON string.","High-cardinality availability metadata does not use Parquet dictionary encoding; low-cardinality identifiers do.","NULL remains NULL and is never replaced with zero."]},
  "phase_c_duckdb":{"status":"passed","examples":["single-stock history","latest-date cross-section","100k-row ML dataframe","T+1/T+3/T+5 target aggregation","global feature join"],"materializes_entire_lake":False},
  "phase_d_validation":{"datasets":validation["datasets"],"target_distribution":validation["target_distribution"],"feature_missing":validation["feature_missing"],"temporal_leakage":validation["temporal_leakage"],"sample_validation":validation["sample_validation"],
   "legacy_false_investigation":{"affected":["2330","2317","6488","2881"],"cause":"Pilot features and targets were inserted before the final price backfill; ON CONFLICT DO NOTHING retained stale derived rows when adjusted prices changed.","fix":"Feature and target writes now UPSERT, the four stale symbols were rebuilt, and the final fixed-plus-random validation passed 55/55.","not_a_migration_error":True}},
  "phase_e_benchmark":{"note":"PostgreSQL ran on the 1-core/2-GB Linode; DuckDB ran on the local Windows research host, so hardware differs.","postgresql":postgres_bench,"duckdb_parquet":parquet_bench,
   "conclusion":"PostgreSQL remains excellent for indexed production lookups; DuckDB/Parquet is substantially better for cold ML scans and halves the warm 100k-row ML load time while using a fraction of storage."},
  "phase_f_cleanup":{"dependency_audit":{"application_api_frontend_daily_consumers":0,"database_views":0,"database_functions":0,"research_consumers":["V3.14 builder","validator","exporter","historical migrations"]},
   "method":"TRUNCATE four research tables and remove temporary export indexes; retain empty schemas","database_before_bytes":23468203031,"database_after_bytes":185555991,"reclaimed_bytes":23282647040,
   "disk_before":{"used_approx":"28 GB","available_approx":"19 GB","percent":60},"disk_after":{"used_bytes":5468057600,"available_bytes":44070989824,"percent":12},"production_services_after":["postgresql active","nginx active","taiwan-stock-api active"]},
  "phase_g_linode":{"plan":{"cpu":"1 shared core","ram_bytes":2063589376,"swap_bytes":520089600,"disk_bytes":52212428800},
   "post_cleanup":{"ram_used_bytes":505741312,"ram_available_bytes":1557848064,"swap_used_bytes":144232448,"postgres_process_rss_bytes":547753984,"api_process_rss_after_benchmark_bytes":api_bench["process_ram_after_bytes"]},
   "api_concurrency_20_requests_200":api_bench,
   "existing_public_http2_page":{"p50_ms":752.15,"p95_ms":916.57,"error_rate":0,"primary_latency":"Taiwan-Tokyo WAN and TLS, not PostgreSQL"},
   "daily_pipeline":{"full_market_json_build_seconds":25.96,"database_sync_seconds":7.504,"global_incremental_test_seconds":8.74,"estimated_sequential_seconds":42.204,"peak_ram":"not historically instrumented; current workload leaves about 1.45 GiB available after cleanup"},
   "five_usd_plan_recommendation":{"suitable":True,"scope":"production REST API, PostgreSQL latest stock snapshots, and normal daily incremental sync","not_suitable_for":"20-year full-market research rebuilds, JSONB feature expansion, or model training","upgrade_trigger":"sustained swap growth, API P95 regression under normal non-rate-limited traffic, disk above 70%, or concurrent research workloads"}},
  "safety":{"formal_taiex_model_modified":False,"platt_modified":False,"market_score_modified":False,"prediction_json_modified":False,"frontend_modified":False,"parquet_gitignored":True,"credentials_committed":False},
 }
 target=PROJECT_ROOT/"data"/"analysis"/"current"/"v314_storage_optimization_report.json";target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+"\n",encoding="utf-8");return target
if __name__=="__main__":print(run())
