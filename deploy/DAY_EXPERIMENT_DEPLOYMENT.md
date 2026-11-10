# Independent DAY 08:15 shadow deployment preparation

This is an experiment deployment only. It must use `/opt/taiwan-stock-day-experiment`, never the production web checkout or the B4 experiment root.

## Release layout

```text
/opt/taiwan-stock-day-experiment/
  releases/<immutable-release>/
  current -> releases/<immutable-release>
  previous -> releases/<prior-release>
  venv/
  data_lake/                 # independent copied/synchronised input data
  state/day_0815_forward/    # append-only predictions/evaluations/artifacts
  state/day_0815_live_snapshots/
  readonly-api/day-shadow.json
```

Before enabling timers, point `current/data_lake` at the independent `../data_lake` directory (a release-local symlink). This makes the existing updater and PIT builder operate only on the experiment copy. Verify this with `readlink -f current/data_lake`; it must not resolve under `/opt/taiwan_stock_analysis_platform`.

When updating a release, deploy source files only. Never copy a workstation's
`day_0815_live_snapshots`, `day_0815_forward`, predictions, evaluations, or
artifacts into the Linode state tree. Existing Linode state is append-only.
If a same-date immutable snapshot already exists, the pre-open job validates
its date, provenance, temporal status, and SHA-256, logs `reused_existing`,
and exits normally without replacing it.

## Timer schedule

- `taiwan-stock-day-experiment-postclose.timer`: 16:00 Asia/Taipei. Fetches only that completed TX daily session. A missing official publication appends a `PENDING` event and exits 75; systemd retries every 15 minutes, at most eight starts per six hours.
- `taiwan-stock-day-experiment-preopen.timer`: 08:05 Asia/Taipei. Builds an immutable DAY snapshot and runs Dummy, Logistic, and HGB before the 08:15 cutoff. Holiday runs are skipped.

Install only after a separate deployment review: copy the four unit files to `/etc/systemd/system`, run `systemd-analyze verify`, then `systemctl daemon-reload`. Do not enable or start them as part of source preparation.

## Rollback

Stop only the two DAY experiment timers/services, repoint `current` to `previous`, and restart the DAY timers after validation. State is append-only and is not rolled back or modified.
