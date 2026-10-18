# Manual MER processing

Admin MER exposes **MER ALL**, an explicit POST action. Opening the page or
polling its status never starts processing.

The pipeline runs these steps sequentially:

1. Scan the CCP MER archive catalog.
2. Download missing ZIPs.
3. Analyze local archives.
4. Import the kill dumps.
5. Import economic data and wait for that worker's successful completion.
6. Resolve historical alliance IDs.
7. Reconcile MER rows with existing raw killmails.

A failed or busy step stops the pipeline. Existing individual actions remain
available when the pipeline is idle. MER ALL refuses to start while an existing
MER worker is active.

The page polls `/admin/mer/status` every three seconds. Progress includes the
current step/month, completed month/file counts, matching counters, start/end
times and errors. PostgreSQL lock waits identify a blocking MER operation when
its backend is known. A bounded tail of `mer.log` is available on the page.
The existing economic import panel provides that worker's detailed counters.
All status and log routes require `admin.mer.view`.

Only a small latest-status JSON snapshot is retained in
`data/mer/operations_progress.json`; this is not a history of all runs. Historical
alliance updates report each partition while retaining their single final
transaction commit. The operation becomes completed only after that commit.
An application restart interrupts in-process work; a later status read marks a
dead worker as interrupted. Do not restart the application during an active run.

Offline checks:

```sh
python -m unittest scripts.test_mer_progress scripts.test_mer_economy_admin scripts.test_mer_economy
python -m scripts.test_mer_economy_admin_browser
```

Economic SQL tests additionally require an explicitly configured disposable
PostgreSQL socket (`EVEOSINT_FORENSICS_TEST_SOCKET`, port 55444).
