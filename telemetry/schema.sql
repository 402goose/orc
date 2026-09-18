-- Applied automatically on server startup (see main.go). Kept here as the
-- readable source of truth; there is no separate migration tool at this
-- scale (a handful of known collaborators).

CREATE TABLE IF NOT EXISTS spans (
    id BIGSERIAL PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    install_id TEXT NOT NULL,
    trace_id TEXT,
    span_id TEXT,
    parent_span_id TEXT,
    agent TEXT,
    role TEXT,
    route TEXT,
    model TEXT,
    is_write BOOLEAN,
    status TEXT,
    failure_class TEXT,
    start_time_ms BIGINT,
    end_time_ms BIGINT,
    duration_ms BIGINT,
    usage JSONB
);

CREATE INDEX IF NOT EXISTS spans_install_id_idx ON spans (install_id);
CREATE INDEX IF NOT EXISTS spans_received_at_idx ON spans (received_at);
CREATE INDEX IF NOT EXISTS spans_agent_status_idx ON spans (agent, status);
