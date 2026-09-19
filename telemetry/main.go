// orc-telemetry is a minimal ingestion endpoint for Fusion's opt-in remote
// telemetry (see fusion_core.py: send_remote_telemetry). It accepts a small,
// deliberately reduced batch of span records -- no prompts, no file paths,
// no raw blocker text -- and stores them in Postgres for later analysis.
//
// This is sized for a handful of known collaborators, not public internet
// scale: no rate limiting, no per-install quotas, a single shared bearer
// token rather than per-user auth.
package main

import (
	"context"
	"database/sql"
	_ "embed"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"
)

//go:embed schema.sql
var schemaSQL string

const (
	maxBodyBytes   = 1 << 20 // 1MB: generous for a batch of a few dozen spans, small enough to bound abuse.
	maxSpansPerReq = 500
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

type server struct {
	db    *sql.DB
	token string
}

func main() {
	databaseURL := os.Getenv("DATABASE_URL")
	if databaseURL == "" {
		log.Fatal("DATABASE_URL is required")
	}
	token := os.Getenv("INGEST_TOKEN")
	if token == "" {
		log.Fatal("INGEST_TOKEN is required -- refusing to run an ingestion endpoint with no shared secret")
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
	if !validBearerToken(r.Header.Get("Authorization"), s.token) {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}

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
	if payload.InstallID == "" {
		http.Error(w, "install_id is required", http.StatusBadRequest)
		return
	}
	if len(payload.Spans) == 0 {
		w.WriteHeader(http.StatusAccepted)
		return
	}
	if len(payload.Spans) > maxSpansPerReq {
		http.Error(w, "too many spans in one batch", http.StatusBadRequest)
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

func validBearerToken(header, expected string) bool {
	const prefix = "Bearer "
	if !strings.HasPrefix(header, prefix) {
		return false
	}
	return header[len(prefix):] == expected
}
