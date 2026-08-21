# PostgreSQL migrations

`001_initial.sql` is applied by `sync_stock_database.py --init-schema` and
recorded in `schema_migrations`. Run migrations with a dedicated migration role
that owns the schema. The application role used by scheduled syncs should only
receive `SELECT, INSERT, UPDATE` on these tables and sequence usage.

Example (execute as the database owner and replace the role name):

```sql
GRANT USAGE ON SCHEMA public TO stock_sync_app;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO stock_sync_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO stock_sync_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE ON TABLES TO stock_sync_app;
```

Do not grant the scheduled application role `SUPERUSER`, `CREATEDB`, schema
ownership, or `DROP` privileges.
