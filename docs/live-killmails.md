# Sequential live killmail import

The Git update installs and enables `eveosint-live-killmails.service`. The worker runs as the application user, independently of the web process. Admin → Update Pipeline shows its current phase, sequence, killmail and counters, with cooperative Pause/Resume controls. A pause finishes the current killmail.

The consumer reads the official R2Z2 sequence feed. For each sequence it imports the complete killmail, checks unknown/changed character and corporation histories using the existing ESI functions, values it with the existing loss-date formula and local abyssal equivalents, and calculates damage shares with NPC damage in the denominator. The existing killboard/statistics queries derive True ISK from the stored value and damage; no second permanent copy of credits is stored.

Exactly one `pending.json` is kept during processing. After all required work (or explicit delegation to the daily job's active lists), the database cursor is committed, then the JSON is deleted. A crash before commit retries the same kill; a crash after commit only removes the stale JSON. Imports and ESI history updates are idempotent. Transient errors retain the current JSON and retry; later sequences are never processed in parallel.

Daily coordination:

* Archive refresh and archive import acquire an exclusive boundary and wait for the current live kill to finish. Other daily stages run concurrently.
* The live consumer refreshes a pilot history at most once per hour after a successful check, even when subsequent KMs still disagree with the cached CCP history. This sparse database timestamp survives restarts. Daily affiliation jobs are not subject to this live-only limit and do not reset its timer. Failed refreshes remain retryable.
* Pending/running character and corporation work belonging to a running affiliation job is left to that job. Dead process records do not suppress live checks.
* The calculator briefly records a per-month import-time cutoff, then releases the boundary. This fixes its work set without copying every kill ID; subsequent live imports remain live-owned. Reservations are ignored after the calculator's database lock disappears.
* Live estimates are marked for reconciliation with the completed loss-date daily prices. The daily archive import preserves already imported kills and their valuations.
* All EVEOSINT ESI entry points share a file-locked rolling-minute budget, capped at the existing 280 requests/minute. Lower active affiliation-job limits are also respected. zKill feed requests use a separate limit and wait at least six seconds after 404.
* During the first upgrade, a pre-upgrade daily process is allowed to finish before starting live, since that old process cannot participate in the new shared quota. Subsequent daily jobs do not require this full-run pause.

Deployment obtains the same boundary before checkout. The worker waits during deployment maintenance, and the installer restarts it on the new code. Rolling back to a version without the consumer stops it; the systemd path condition prevents a restart loop. The daily process is not stopped.

The durable cursor lives in `rawkm.live_killmail_cursor`. If its feed position has expired after a long interruption, the consumer records the missing sequence interval and resumes from an available pointer. The existing daily archives reconcile any missing kills. No archive or killmail data is deleted by the live service.

Upstream protocol: https://zkillboard.com/api/docs/ (Live feed & archives).
