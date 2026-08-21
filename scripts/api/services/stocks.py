"""Read-only stock queries and API response mapping."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from api.db import pool


def value(item: Any) -> Any:
    if isinstance(item, Decimal):
        return int(item) if item == item.to_integral() else float(item)
    if isinstance(item, (date, datetime)):
        return item.isoformat()
    return item


def clean(row: dict[str, Any] | None) -> dict[str, Any] | None:
    return {key: value(item) for key, item in row.items()} if row else None


def one(cursor, query: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
    cursor.execute(query, parameters)
    return clean(cursor.fetchone())


def search_stocks(search: str | None, market: str | None, industry: str | None, limit: int) -> list[dict[str, Any]]:
    clauses = ["active = true"]
    parameters: list[Any] = []
    if search:
        clauses.append("(symbol ILIKE %s OR name ILIKE %s)")
        pattern = f"%{search}%"; parameters.extend((pattern, pattern))
    if market:
        clauses.append("market = %s"); parameters.append(market)
    if industry:
        clauses.append("industry = %s"); parameters.append(industry)
    parameters.append(limit)
    with pool.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT symbol,name,market,industry,instrument_type,active
               FROM stocks WHERE """ + " AND ".join(clauses) +
            " ORDER BY CASE WHEN symbol=%s THEN 0 ELSE 1 END, symbol LIMIT %s"
            if search else
            """SELECT symbol,name,market,industry,instrument_type,active
               FROM stocks WHERE """ + " AND ".join(clauses) + " ORDER BY symbol LIMIT %s",
            tuple(parameters[:-1] + ([search, parameters[-1]] if search else [parameters[-1]])),
        )
        return [clean(row) for row in cursor.fetchall()]


def stock_exists(cursor, symbol: str) -> dict[str, Any] | None:
    return one(cursor, """SELECT stock_id,symbol,name,market,industry,instrument_type,active,
                                  cached,cache_status,source_updated_at
                           FROM stocks WHERE symbol=%s""", (symbol,))


def financial_rows(cursor, stock_id: int, limit: int) -> list[dict[str, Any]]:
    cursor.execute(
        """SELECT fiscal_year,quarter,statement_scope,period_end,published_date,available_date,
                  eps,revenue,gross_margin,operating_margin,net_margin,roe,total_assets,
                  total_liabilities,debt_ratio,current_ratio,operating_cash_flow,
                  investing_cash_flow,capital_expenditure,free_cash_flow,monetary_unit,source
           FROM stock_financials WHERE stock_id=%s
           ORDER BY period_end DESC LIMIT %s""", (stock_id, limit),
    )
    return [clean(row) for row in cursor.fetchall()]


def chips_rows(cursor, stock_id: int, limit: int) -> list[dict[str, Any]]:
    cursor.execute(
        """SELECT trade_date,foreign_net,investment_trust_net,dealer_net,institutional_total,
                  margin_balance,margin_change,short_balance,short_change,
                  institutional_unit,margin_unit
           FROM stock_chips WHERE stock_id=%s ORDER BY trade_date DESC LIMIT %s""",
        (stock_id, limit),
    )
    return [clean(row) for row in cursor.fetchall()]


def get_financials(symbol: str, limit: int) -> dict[str, Any] | None:
    with pool.connection() as connection, connection.cursor() as cursor:
        stock = stock_exists(cursor, symbol)
        if not stock: return None
        rows = financial_rows(cursor, stock["stock_id"], limit)
        return {"symbol": symbol, "count": len(rows), "financials": rows}


def get_chips(symbol: str, limit: int) -> dict[str, Any] | None:
    with pool.connection() as connection, connection.cursor() as cursor:
        stock = stock_exists(cursor, symbol)
        if not stock: return None
        rows = chips_rows(cursor, stock["stock_id"], limit)
        return {"symbol": symbol, "count": len(rows), "chips": rows}


def get_stock(symbol: str) -> dict[str, Any] | None:
    with pool.connection() as connection, connection.cursor() as cursor:
        stock = stock_exists(cursor, symbol)
        if not stock: return None
        stock_id = stock.pop("stock_id")
        quote = one(cursor, """SELECT trade_date,close,change,change_percent,volume,price_unit,volume_unit
                               FROM stock_quotes WHERE stock_id=%s ORDER BY trade_date DESC LIMIT 1""", (stock_id,))
        valuation = one(cursor, """SELECT valuation_date,pe,pb,dividend_yield,pe_date,pb_date,dividend_yield_date
                                   FROM stock_valuations WHERE stock_id=%s ORDER BY valuation_date DESC LIMIT 1""", (stock_id,))
        cursor.execute(
            """SELECT valuation_date AS trade_date,pe,pb,dividend_yield
               FROM stock_valuations WHERE stock_id=%s ORDER BY valuation_date""", (stock_id,),
        )
        valuation_observations = [clean(row) for row in cursor.fetchall()]
        revenue = one(cursor, """SELECT revenue_month,revenue,revenue_yoy,revenue_mom,unit
                                 FROM stock_monthly_revenue WHERE stock_id=%s ORDER BY revenue_month DESC LIMIT 1""", (stock_id,))
        financials = financial_rows(cursor, stock_id, 1)
        chips = chips_rows(cursor, stock_id, 60)
        cursor.execute(
            """SELECT category,metric_key,source_date,value_numeric,value_text,threshold_text,status,unit,note,rules_version,
                      source_payload->>'label' AS label
               FROM stock_health WHERE stock_id=%s ORDER BY as_of_date DESC,category,metric_key""", (stock_id,),
        )
        health: dict[str, list[dict[str, Any]]] = defaultdict(list)
        rules_version = None
        seen: set[tuple[str, str]] = set()
        for row in cursor.fetchall():
            key = (row["category"], row["metric_key"])
            if key in seen: continue
            seen.add(key); rules_version = rules_version or row["rules_version"]
            health[row["category"]].append({
                "key": row["metric_key"], "label": row["label"] or row["metric_key"],
                "value": value(row["value_numeric"]) if row["value_numeric"] is not None else row["value_text"],
                "status": row["status"], "threshold": row["threshold_text"], "unit": row["unit"],
                "note": row["note"], "data_date": value(row["source_date"]),
            })
        cursor.execute(
            """SELECT industry,metric_key,comparison_period,company_value,industry_median,
                      percentile,rank,sample_size,relative_status
               FROM industry_rankings WHERE stock_id=%s ORDER BY metric_key""", (stock_id,),
        )
        peers = [clean(row) for row in cursor.fetchall()]
        latest_financial = financials[0] if financials else None
        fundamentals = None
        if latest_financial or revenue:
            fundamentals = {"report_period": f"{latest_financial['fiscal_year']}Q{latest_financial['quarter']}" if latest_financial else None,
                            "report_date": latest_financial.get("period_end") if latest_financial else None,
                            "available_date": latest_financial.get("available_date") if latest_financial else None,
                            **({key: latest_financial.get(key) for key in ("eps","roe","gross_margin","operating_margin","net_margin","debt_ratio","current_ratio","operating_cash_flow","free_cash_flow")} if latest_financial else {}),
                            "revenue": revenue}
        chronological = list(reversed(chips))
        def total(field: str, days: int) -> int | float | None:
            vals = [row[field] for row in chronological[-days:] if row.get(field) is not None]
            return sum(vals) if vals else None
        chips_summary = {"trade_date": chips[0]["trade_date"] if chips else None,
                         "latest": chips[0] if chips else None,
                         "foreign_5d": total("foreign_net", 5), "foreign_20d": total("foreign_net", 20),
                         "investment_trust_5d": total("investment_trust_net", 5),
                         "investment_trust_20d": total("investment_trust_net", 20),
                         "available_days": len(chips)}
        return {
            "profile": {key: stock.get(key) for key in ("symbol","name","market","industry","instrument_type","active")},
            "quote": quote, "valuation": valuation,
            "valuation_history_observations": valuation_observations,
            "fundamentals": fundamentals,
            "health_v2": {"applicable": stock["instrument_type"] == "company", "rules_version": rules_version, "categories": dict(health)},
            "peer_analysis": {"applicable": stock["instrument_type"] == "company", "metrics": peers},
            "chips": {"summary": chips_summary},
            "build_status": {"state": stock.get("cache_status"), "cached": stock.get("cached"), "updated_at": stock.get("source_updated_at")},
        }


def get_industry_peers(industry: str) -> dict[str, Any]:
    with pool.connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) AS count FROM stocks WHERE industry=%s AND active=true", (industry,))
        company_count = cursor.fetchone()["count"]
        cursor.execute(
            """SELECT r.metric_key,r.comparison_period,s.symbol,s.name,r.company_value,
                      r.industry_median,r.percentile,r.rank,r.sample_size,r.relative_status
               FROM industry_rankings r JOIN stocks s USING(stock_id)
               WHERE r.industry=%s ORDER BY r.metric_key,r.rank NULLS LAST,s.symbol""", (industry,),
        )
        rows = [clean(row) for row in cursor.fetchall()]
        cursor.execute(
            """WITH target_stocks AS MATERIALIZED (
                   SELECT stock_id,symbol,name,market,industry,instrument_type
                   FROM stocks
                   WHERE industry=%s AND active=true AND instrument_type='company'
               ), latest_valuation AS (
                   SELECT DISTINCT ON (v.stock_id) v.stock_id,v.valuation_date,v.pe,v.pb,v.dividend_yield
                   FROM stock_valuations v JOIN target_stocks t USING(stock_id)
                   ORDER BY v.stock_id,v.valuation_date DESC
               ), latest_revenue AS (
                   SELECT DISTINCT ON (r.stock_id) r.stock_id,r.revenue_month,r.revenue_yoy
                   FROM stock_monthly_revenue r JOIN target_stocks t USING(stock_id)
                   ORDER BY r.stock_id,r.revenue_month DESC
               ), health AS (
                   SELECT h.stock_id,
                     max(value_numeric) FILTER (WHERE metric_key='roe') AS roe,
                     max(value_numeric) FILTER (WHERE metric_key='eps_yoy') AS eps_yoy,
                     max(value_numeric) FILTER (WHERE metric_key='ttm_eps') AS ttm_eps,
                     max(value_numeric) FILTER (WHERE metric_key='ttm_operating_cash_flow') AS ttm_operating_cash_flow,
                     max(value_numeric) FILTER (WHERE metric_key='ttm_free_cash_flow') AS ttm_free_cash_flow,
                     max(source_date) AS financial_date
                   FROM stock_health h JOIN target_stocks t USING(stock_id)
                   GROUP BY h.stock_id
               )
               SELECT s.symbol,s.name,s.market,s.industry,s.instrument_type,
                      v.pe,v.pb,v.dividend_yield,v.valuation_date,
                      r.revenue_yoy,r.revenue_month AS revenue_period,
                      h.roe,h.eps_yoy,h.ttm_eps,h.ttm_operating_cash_flow,
                      h.ttm_free_cash_flow,h.financial_date
               FROM target_stocks s
               LEFT JOIN latest_valuation v USING(stock_id)
               LEFT JOIN latest_revenue r USING(stock_id)
               LEFT JOIN health h USING(stock_id)
               ORDER BY s.symbol""", (industry,),
        )
        peer_stocks = [clean(row) for row in cursor.fetchall()]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows: grouped[row.pop("metric_key")].append(row)
    return {"industry": industry, "company_count": company_count,
            "ranking_record_count": len(rows), "rankings": dict(grouped),
            "stocks": peer_stocks}
