# orc-telemetry

Ingestion endpoint for Fusion's opt-in remote telemetry. Receives a
deliberately reduced batch of span records from `fusion` clients that have
`telemetry.remote.enabled: true` in their `.fusion.json`, and stores them in
Postgres.

Sized for a handful of known collaborators (a few people, not public
internet scale): a single shared bearer token, no per-install rate limiting,
no multi-tenant auth.

## What gets sent

See `fusion_core.py`'s `send_remote_telemetry()` for the authoritative
client-side list. Per dispatch: agent, role, route, model, write flag,
status, a coarse `failure_class` (`quota` / `permission_denied` / `timeout`
/ `missing_executable` / `worker_error` — never raw blocker text), start/end
timestamps, duration, and token/cost usage. Plus a random per-machine
`install_id` that is not tied to identity.

Never sent, even when remote telemetry is enabled: prompts, model output,
changed file paths, test commands, raw blocker text, local filesystem
paths, workspace/repo names.

## Local development

```sh
go run .
```

Requires `DATABASE_URL` (a Postgres connection string) and `INGEST_TOKEN`
(the shared bearer secret) in the environment; refuses to start without
either. `PORT` defaults to 8080.

```sh
go build ./...
go vet ./...
gofmt -l .   # should print nothing
```

## Deploy (first time)

```sh
fly launch --no-deploy --copy-config --name orc-telemetry
fly postgres create --name orc-telemetry-db
fly postgres attach orc-telemetry-db --app orc-telemetry   # sets DATABASE_URL secret
fly secrets set INGEST_TOKEN="$(openssl rand -hex 32)" --app orc-telemetry
fly deploy
```

`fly postgres attach` sets `DATABASE_URL` as a Fly secret automatically. The
schema (`schema.sql`, embedded into the binary) applies itself on startup —
there is no separate migration step at this scale.

## Deploy (updates)

```sh
fly deploy
```

## Client setup

None. Reporting is on by default and needs no token: `fusion` ships with
this endpoint in its defaults, announces itself on the first send, and
sends the reduced payload above. Ingest takes no credential deliberately —
a shared write secret shipped in a public repo protects nothing and still
has to be rotated when it leaks.

To stop reporting, either of:

```sh
export FUSION_TELEMETRY=0          # stops the send, keeps local traces
```

```json
{ "telemetry": { "remote": { "enabled": false } } }
```

`telemetry.enabled: false` stops the local trace as well, which also turns
off lane cooldown, resume accounting and `fusion usage` — usually not what
you want.

Run `fusion telemetry status` in any workspace to see exactly what will be
sent, or confirm it's off.

The token still exists, for reading: `GET /v1/summary` requires it, so
aggregate usage is not public. Contributors never need it.

## Querying

```sh
fly postgres connect --app orc-telemetry-db
```

```sql
select agent, route, model, status, failure_class, count(*), avg(duration_ms)
from spans
group by 1,2,3,4,5
order by count(*) desc;
```
