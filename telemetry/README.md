# orc-telemetry

Ingestion endpoint for Fusion's opt-in remote telemetry. Receives a
deliberately reduced batch of span records from `fusion` clients that have
`telemetry.remote.enabled: true` in their `.fusion.json`, and stores them in
Postgres.

**Deployed:** `https://orc-telemetry.fly.dev` (Fly app `orc-telemetry`,
Postgres cluster `orc-telemetry-db`, org `jfl`). Verified live end to end: `/healthz` returns 200, `/v1/ingest`
rejects a wrong bearer token with 401, a real authenticated payload returns
202, and the row was then read back out of the cluster carrying exactly the
reduced fields (`install_id | agent | status | duration_ms`). The 202 is
itself evidence of the write — `handleIngest` calls `insertSpans`
synchronously and returns 500 on failure — but it was confirmed against the
database rather than inferred from the code path.

If `orc-telemetry.fly.dev` doesn't resolve from a given machine but `fly
status` shows the app healthy, check whether Tailscale's MagicDNS resolver
(`100.100.100.100`) is intercepting DNS and not forwarding `fly.dev` --
`nslookup orc-telemetry.fly.dev 8.8.8.8` will resolve correctly even when
the default resolver doesn't.

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

# Write the token down BEFORE setting it. Fly secrets are write-only:
# `fly secrets list` shows a digest, never the value, so a token generated
# inline is unrecoverable and every client is locked out until it is rotated.
( umask 077; openssl rand -hex 32 > ~/.config/orc/telemetry-ingest-token )
fly secrets set INGEST_TOKEN="$(cat ~/.config/orc/telemetry-ingest-token)" --app orc-telemetry

fly deploy
```

To rotate it, repeat those two commands: setting the secret restarts the
machines, and every client's `.fusion.json` needs the new value. A client
still holding the old token gets a 401 and, because the send is best-effort,
silently stops contributing.

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

The easy way, from any machine that has the shared token (no Fly/DB access
needed):

```sh
fusion telemetry report              # last 7 days
fusion telemetry report --hours 24   # last 24 hours
fusion --json telemetry report       # machine-readable
```

This hits `GET /v1/summary` on the collector, which returns spans grouped
by agent/route/model/status/failure_class with call counts, total cost, and
average duration, plus a distinct-install count -- so anyone with the
shared token can see the group's aggregate patterns without ever touching
Postgres directly.

For anything the summary endpoint doesn't cover, direct SQL still works for
whoever has `jfl` org access. `fly postgres connect` opens an interactive
`psql`; it ignores `-c` and hangs when driven non-interactively, so for a
one-shot query go through the database machine instead (the password expands
on the remote host and never reaches your shell history):

```sh
fly ssh console --app orc-telemetry-db -C 'sh -c "psql \
  \"postgres://postgres:\$OPERATOR_PASSWORD@127.0.0.1:5433/orc_telemetry\" \
  -A -t -c \"SELECT count(*) FROM spans\""'
```

Interactively:

```sh
fly postgres connect --app orc-telemetry-db
```

```sql
select agent, route, model, status, failure_class, count(*), avg(duration_ms)
from spans
group by 1,2,3,4,5
order by count(*) desc;
```
