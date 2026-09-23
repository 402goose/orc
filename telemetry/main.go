// orc-telemetry is a minimal ingestion endpoint for Fusion's default-on remote
// telemetry (see fusion_core.py: send_remote_telemetry). It accepts a small,
// deliberately reduced batch of span records -- no prompts, no file paths,
// no raw blocker text -- and stores them in Postgres for later analysis.
//
// This is sized for a handful of known collaborators, not public internet
// scale: no rate limiting, no per-install quotas, a single shared bearer
// token for reading summaries rather than per-user auth. Ingest is open.
package main

import (
	"context"
	"crypto/subtle"
	"database/sql"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"
)

//go:embed schema.sql
var schemaSQL string

const (
	maxBodyBytes       = 1 << 20 // 1MB: generous for a batch of a few dozen spans, small enough to bound abuse.
	maxSpansPerReq     = 500
	defaultWindowHours = 168 // 7 days
	maxWindowHours     = 24 * 90
)

type span struct {
	TraceID      *string         `json:"trace_id"`
	SpanID       *string         `json:"span_id"`
	ParentSpanID *string         `json:"parent_span_id"`
	Agent        *string         `json:"agent"`
	Role         *string         `json:"role"`
	Route        *string         `json:"route"`
	Model        *string         `json:"model"`
	Write        *bool           `json:"write"`
	Status       *string         `json:"status"`
	FailureClass *string         `json:"failure_class"`
	StartTimeMs  *int64          `json:"start_time_ms"`
	EndTimeMs    *int64          `json:"end_time_ms"`
	DurationMs   *int64          `json:"duration_ms"`
	Usage        json.RawMessage `json:"usage"`
}

type ingestPayload struct {
	Schema    string `json:"schema"`
	InstallID string `json:"install_id"`
	Spans     []span `json:"spans"`
}

type summaryRow struct {
	Agent             *string `json:"agent"`
	Route             *string `json:"route"`
	Model             *string `json:"model"`
	Status            *string `json:"status"`
	FailureClass      *string `json:"failure_class"`
	Calls             int64   `json:"calls"`
	TotalCostUSD      float64 `json:"total_cost_usd"`
	TotalInputTokens  float64 `json:"total_input_tokens"`
	TotalOutputTokens float64 `json:"total_output_tokens"`
	AvgDurationMs     float64 `json:"avg_duration_ms"`
}

type summaryResponse struct {
	WindowHours    int          `json:"window_hours"`
	TotalSpans     int64        `json:"total_spans"`
	UniqueInstalls int64        `json:"unique_installs"`
	ByGroup        []summaryRow `json:"by_group"`
}

type server struct {
	db    *sql.DB
	token string
}

func main() {
	databaseURL := os.Getenv("DATABASE_URL")
	if databaseURL == "" {
		log.Fatal("DATABASE_URL is required")
	}
	// Guards the read side only. Ingest is deliberately open: clients ship
	// with telemetry on by default, and a shared write secret distributed
	// through a public repo would protect nothing while still needing to be
	// rotated. Reading what everyone sent stays behind this.
	token := os.Getenv("INGEST_TOKEN")
	if token == "" {
		log.Fatal("INGEST_TOKEN is required -- refusing to expose read endpoints with no shared secret")
	}
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}

	db, err := sql.Open("pgx", databaseURL)
	if err != nil {
		log.Fatalf("open database: %v", err)
	}
	defer db.Close()
	db.SetMaxOpenConns(5)

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := migrate(ctx, db); err != nil {
		log.Fatalf("migrate: %v", err)
	}

	srv := &server{db: db, token: token}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", srv.handleHealthz)
	mux.HandleFunc("POST /v1/ingest", srv.handleIngest)
	mux.HandleFunc("GET /v1/summary", srv.handleSummary)

	log.Printf("orc-telemetry listening on :%s", port)
	if err := http.ListenAndServe(":"+port, mux); err != nil {
		log.Fatal(err)
	}
}

func migrate(ctx context.Context, db *sql.DB) error {
	_, err := db.ExecContext(ctx, schemaSQL)
	return err
}

func (s *server) handleHealthz(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()
	if err := s.db.PingContext(ctx); err != nil {
		http.Error(w, "database unavailable", http.StatusServiceUnavailable)
		return
	}
	w.WriteHeader(http.StatusOK)
}

func (s *server) handleIngest(w http.ResponseWriter, r *http.Request) {
	// No auth by design -- see the note in main(). The body size cap and the
	// payload validation below are what stand between this and junk.
	r.Body = http.MaxBytesReader(w, r.Body, maxBodyBytes)
	body, err := io.ReadAll(r.Body)
	if err != nil {
		var maxErr *http.MaxBytesError
		if errors.As(err, &maxErr) {
			http.Error(w, "payload too large", http.StatusRequestEntityTooLarge)
			return
		}
		http.Error(w, "cannot read body", http.StatusBadRequest)
		return
	}

	var payload ingestPayload
	if err := json.Unmarshal(body, &payload); err != nil {
		http.Error(w, "invalid json", http.StatusBadRequest)
		return
	}
	if err := validateIngestPayload(payload); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	if len(payload.Spans) == 0 {
		w.WriteHeader(http.StatusAccepted)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	if err := s.insertSpans(ctx, payload.InstallID, payload.Spans); err != nil {
		log.Printf("insert spans: %v", err)
		http.Error(w, "storage error", http.StatusInternalServerError)
		return
	}
	w.WriteHeader(http.StatusAccepted)
}

// validateIngestPayload is pure (no I/O) so it can be unit tested without a
// database.
func validateIngestPayload(payload ingestPayload) error {
	if payload.InstallID == "" {
		return errors.New("install_id is required")
	}
	if len(payload.Spans) > maxSpansPerReq {
		return errors.New("too many spans in one batch")
	}
	return nil
}

func (s *server) handleSummary(w http.ResponseWriter, r *http.Request) {
	if !validBearerToken(r.Header.Get("Authorization"), s.token) {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	hours, err := parseWindowHours(r.URL.Query().Get("hours"))
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	resp, err := s.querySummary(ctx, hours)
	if err != nil {
		log.Printf("query summary: %v", err)
		http.Error(w, "storage error", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(resp); err != nil {
		log.Printf("encode summary: %v", err)
	}
}

// parseWindowHours is pure so it can be unit tested without a server.
func parseWindowHours(raw string) (int, error) {
	if raw == "" {
		return defaultWindowHours, nil
	}
	hours, err := strconv.Atoi(raw)
	if err != nil || hours <= 0 || hours > maxWindowHours {
		return 0, fmt.Errorf("hours must be a positive integer up to %d", maxWindowHours)
	}
	return hours, nil
}

func (s *server) insertSpans(ctx context.Context, installID string, spans []span) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()

	stmt, err := tx.PrepareContext(ctx, `
		INSERT INTO spans (
			install_id, trace_id, span_id, parent_span_id, agent, role, route,
			model, is_write, status, failure_class, start_time_ms, end_time_ms,
			duration_ms, usage
		) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
	`)
	if err != nil {
		return err
	}
	defer stmt.Close()

	for _, sp := range spans {
		usage := sp.Usage
		if len(usage) == 0 {
			usage = json.RawMessage("{}")
		}
		if _, err := stmt.ExecContext(ctx,
			installID, sp.TraceID, sp.SpanID, sp.ParentSpanID, sp.Agent, sp.Role,
			sp.Route, sp.Model, sp.Write, sp.Status, sp.FailureClass,
			sp.StartTimeMs, sp.EndTimeMs, sp.DurationMs, usage,
		); err != nil {
			return err
		}
	}
	return tx.Commit()
}

func (s *server) querySummary(ctx context.Context, hours int) (*summaryResponse, error) {
	resp := &summaryResponse{WindowHours: hours}

	totals := s.db.QueryRowContext(ctx, `
		SELECT count(*), count(DISTINCT install_id)
		FROM spans
		WHERE received_at > now() - make_interval(hours => $1)
	`, hours)
	if err := totals.Scan(&resp.TotalSpans, &resp.UniqueInstalls); err != nil {
		return nil, err
	}

	rows, err := s.db.QueryContext(ctx, `
		SELECT agent, route, model, status, failure_class,
		       count(*) AS calls,
		       COALESCE(sum((usage->>'cost_usd')::numeric), 0) AS total_cost_usd,
		       COALESCE(sum((usage->>'input_tokens')::numeric), 0) AS total_input_tokens,
		       COALESCE(sum((usage->>'output_tokens')::numeric), 0) AS total_output_tokens,
		       COALESCE(avg(duration_ms), 0) AS avg_duration_ms
		FROM spans
		WHERE received_at > now() - make_interval(hours => $1)
		GROUP BY agent, route, model, status, failure_class
		ORDER BY calls DESC
		LIMIT 200
	`, hours)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	for rows.Next() {
		var row summaryRow
		if err := rows.Scan(
			&row.Agent, &row.Route, &row.Model, &row.Status, &row.FailureClass,
			&row.Calls, &row.TotalCostUSD, &row.TotalInputTokens, &row.TotalOutputTokens, &row.AvgDurationMs,
		); err != nil {
			return nil, err
		}
		resp.ByGroup = append(resp.ByGroup, row)
	}
	return resp, rows.Err()
}

func validBearerToken(header, expected string) bool {
	if expected == "" {
		// main() already refuses to start with an empty INGEST_TOKEN, but
		// never treat "no token configured" as "anything authenticates".
		return false
	}
	const prefix = "Bearer "
	if !strings.HasPrefix(header, prefix) {
		return false
	}
	provided := header[len(prefix):]
	return subtle.ConstantTimeCompare([]byte(provided), []byte(expected)) == 1
}
