"""Audit database objects that depend on V3.14 research-only relations."""
from __future__ import annotations
import json
from config import PROJECT_ROOT as _PROJECT_ROOT
from database.connection import connect

TABLES=("stock_daily_prices","stock_prediction_features","stock_prediction_targets","stock_industry_daily_features")
def run():
 db=connect();result={}
 try:
  with db.cursor() as cursor:
   for table in TABLES:
    cursor.execute("""
      SELECT dependent_ns.nspname schema_name,dependent.relname object_name,dependent.relkind
      FROM pg_depend dependency
      JOIN pg_rewrite rewrite ON rewrite.oid=dependency.objid
      JOIN pg_class dependent ON dependent.oid=rewrite.ev_class
      JOIN pg_namespace dependent_ns ON dependent_ns.oid=dependent.relnamespace
      WHERE dependency.refobjid=%s::regclass AND dependent.oid<>%s::regclass
      ORDER BY 1,2
    """,(table,table))
    views=[dict(row) for row in cursor.fetchall()]
    cursor.execute("""
      SELECT namespace.nspname schema_name,procedure.proname function_name
      FROM pg_proc procedure JOIN pg_namespace namespace ON namespace.oid=procedure.pronamespace
      WHERE procedure.prokind='f' AND pg_get_functiondef(procedure.oid) ILIKE %s
      ORDER BY 1,2
    """,(f"%{table}%",))
    functions=[dict(row) for row in cursor.fetchall()]
    result[table]={"dependent_views":views,"dependent_functions":functions}
 finally:db.close()
 return result
if __name__=="__main__":print(json.dumps(run(),default=str))
