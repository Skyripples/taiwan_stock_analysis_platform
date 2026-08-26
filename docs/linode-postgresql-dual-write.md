# Linode PostgreSQL and API deployment

PostgreSQL and the Linode REST API are the primary full-market data source.
GitHub retains the complete search index and shared snapshots. Individual stock
JSON fallback files are no longer retained; PostgreSQL and the REST API are the
formal stock-detail source. A database failure must never roll back or delete
an existing database record.

## Roles and network boundary

- Use a dedicated migration role to create/alter schema objects.
- Use a separate, non-superuser application role for scheduled UPSERTs. Grant
  only schema usage, table `SELECT/INSERT/UPDATE`, and sequence usage.
- Bind PostgreSQL to a private interface. Do not allow `0.0.0.0/0` in the Linode
  firewall or `pg_hba.conf`.
- PostgreSQL listens only on localhost. GitHub Actions transfers generated JSON
  over SSH and executes the sync locally as the non-root `stock-sync` account.
- Store `LINODE_HOST`, `LINODE_SSH_PRIVATE_KEY`, and the pinned
  `LINODE_SSH_HOST_KEY` as GitHub Actions secrets. Never commit a private key or
  `.env` file.

## Bootstrap and import

From a trusted host that can reach the private database:

```powershell
python scripts/sync_stock_database.py --init-schema
python scripts/sync_stock_database.py --full
python scripts/validate_database_sync.py
```

The first command should use the migration role. The full import and later
scheduled runs should use the application role. Supported incremental modes are
`--daily`, `--monthly`, and `--quarterly`; `--symbol 2330` limits any mode for a
safe test. Add `--dry-run` to parse and count local JSON without contacting the
database.

The daily workflow builds all stock caches locally, transfers them to Linode,
and performs the PostgreSQL UPSERT before staging Git data. If synchronization
fails, the API keeps its previous valid database state, the status manifest can
still be committed, and the workflow finishes with an
explicit failure. Full-market stock JSON files are never included in the daily
Git staging scope; individual stock JSON files are never staged.
