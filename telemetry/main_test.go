package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"
)

func TestValidBearerToken(t *testing.T) {
	cases := []struct {
		name   string
		header string
		token  string
		want   bool
	}{
		{"correct token", "Bearer sekret", "sekret", true},
		{"wrong token", "Bearer wrong", "sekret", false},
		{"missing header", "", "sekret", false},
		{"missing bearer prefix", "sekret", "sekret", false},
		{"empty expected token never matches", "Bearer ", "", false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := validBearerToken(c.header, c.token); got != c.want {
				t.Errorf("validBearerToken(%q, %q) = %v, want %v", c.header, c.token, got, c.want)
			}
		})
	}
}

func TestValidateIngestPayload(t *testing.T) {
	if err := validateIngestPayload(ingestPayload{InstallID: "abc"}); err != nil {
		t.Errorf("expected no error for a valid payload, got %v", err)
	}
	if err := validateIngestPayload(ingestPayload{}); err == nil {
		t.Error("expected an error when install_id is empty")
	}
	tooMany := make([]span, maxSpansPerReq+1)
	if err := validateIngestPayload(ingestPayload{InstallID: "abc", Spans: tooMany}); err == nil {
		t.Error("expected an error when the batch exceeds maxSpansPerReq")
	}
	exactly := make([]span, maxSpansPerReq)
	if err := validateIngestPayload(ingestPayload{InstallID: "abc", Spans: exactly}); err != nil {
		t.Errorf("expected no error at exactly maxSpansPerReq, got %v", err)
	}
}

func TestParseWindowHours(t *testing.T) {
	if got, err := parseWindowHours(""); err != nil || got != defaultWindowHours {
		t.Errorf("empty input: got (%d, %v), want (%d, nil)", got, err, defaultWindowHours)
	}
	if got, err := parseWindowHours("24"); err != nil || got != 24 {
		t.Errorf("valid input: got (%d, %v), want (24, nil)", got, err)
	}
	for _, bad := range []string{"0", "-1", "not-a-number", "999999"} {
		if _, err := parseWindowHours(bad); err == nil {
			t.Errorf("expected an error for input %q", bad)
		}
	}
}

// testDB opens a connection to TEST_DATABASE_URL and skips the test if it
// isn't set, so `go test ./...` works locally without Postgres while CI
// (which sets it against a real service container) gets full integration
// coverage of the actual SQL, not just the pure-logic paths above.
func testDB(t *testing.T) *sql.DB {
	t.Helper()
	url := os.Getenv("TEST_DATABASE_URL")
	if url == "" {
		t.Skip("TEST_DATABASE_URL not set; skipping database integration test")
	}
	db, err := sql.Open("pgx", url)
	if err != nil {
		t.Fatalf("open database: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := migrate(ctx, db); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	if _, err := db.ExecContext(ctx, "TRUNCATE spans RESTART IDENTITY"); err != nil {
		t.Fatalf("truncate spans: %v", err)
	}
	return db
}

func strPtr(s string) *string { return &s }
func i64Ptr(i int64) *int64   { return &i }

func TestInsertAndQuerySummaryIntegration(t *testing.T) {
	db := testDB(t)
	srv := &server{db: db, token: "test"}
	ctx := context.Background()

	spans := []span{
		{
			Agent: strPtr("claude"), Role: strPtr("implementation"), Route: strPtr("orc-free"),
			Model: strPtr("claude-sonnet-5"), Status: strPtr("success"),
			DurationMs: i64Ptr(1000),
			Usage:      []byte(`{"cost_usd":0.10,"input_tokens":10,"output_tokens":20}`),
		},
		{
			Agent: strPtr("claude"), Role: strPtr("implementation"), Route: strPtr("orc-free"),
			Model: strPtr("claude-sonnet-5"), Status: strPtr("success"),
			DurationMs: i64Ptr(2000),
			Usage:      []byte(`{"cost_usd":0.20,"input_tokens":15,"output_tokens":25}`),
		},
		{
			Agent: strPtr("codex"), Role: strPtr("review"), Status: strPtr("error"),
			FailureClass: strPtr("quota"), DurationMs: i64Ptr(500),
		},
	}
	if err := srv.insertSpans(ctx, "install-a", spans[:2]); err != nil {
		t.Fatalf("insertSpans (install-a): %v", err)
	}
	if err := srv.insertSpans(ctx, "install-b", spans[2:]); err != nil {
		t.Fatalf("insertSpans (install-b): %v", err)
	}

	resp, err := srv.querySummary(ctx, defaultWindowHours)
	if err != nil {
		t.Fatalf("querySummary: %v", err)
	}
	if resp.TotalSpans != 3 {
		t.Errorf("TotalSpans = %d, want 3", resp.TotalSpans)
	}
	if resp.UniqueInstalls != 2 {
		t.Errorf("UniqueInstalls = %d, want 2", resp.UniqueInstalls)
	}
	if len(resp.ByGroup) != 2 {
		t.Fatalf("ByGroup has %d rows, want 2 (claude/success group + codex/error group)", len(resp.ByGroup))
	}

	var claudeGroup, codexGroup *summaryRow
	for i := range resp.ByGroup {
		row := &resp.ByGroup[i]
		if row.Agent != nil && *row.Agent == "claude" {
			claudeGroup = row
		}
		if row.Agent != nil && *row.Agent == "codex" {
			codexGroup = row
		}
	}
	if claudeGroup == nil || codexGroup == nil {
		t.Fatalf("expected both a claude and a codex group, got %+v", resp.ByGroup)
	}
	if claudeGroup.Calls != 2 {
		t.Errorf("claude group calls = %d, want 2", claudeGroup.Calls)
	}
	if claudeGroup.TotalCostUSD < 0.29 || claudeGroup.TotalCostUSD > 0.31 {
		t.Errorf("claude group total cost = %v, want ~0.30", claudeGroup.TotalCostUSD)
	}
	if codexGroup.FailureClass == nil || *codexGroup.FailureClass != "quota" {
		t.Errorf("codex group failure_class = %v, want quota", codexGroup.FailureClass)
	}

	// A zero-hour-old window should report nothing: proves the time filter
	// in the SQL is actually applied, not just present in the query text.
	narrow, err := srv.querySummary(ctx, 1)
	if err != nil {
		t.Fatalf("querySummary(1): %v", err)
	}
	if narrow.TotalSpans != 3 {
		t.Errorf("querySummary(1) TotalSpans = %d, want 3 (rows were just inserted, well within 1 hour)", narrow.TotalSpans)
	}
}

func TestHandleIngestOverHTTP(t *testing.T) {
	db := testDB(t)
	srv := &server{db: db, token: "sekret"}

	post := func(t *testing.T, body string, auth string) *httptest.ResponseRecorder {
		t.Helper()
		req := httptest.NewRequest(http.MethodPost, "/v1/ingest", strings.NewReader(body))
		if auth != "" {
			req.Header.Set("Authorization", auth)
		}
		rec := httptest.NewRecorder()
		srv.handleIngest(rec, req)
		return rec
	}

	if rec := post(t, `not json`, "Bearer sekret"); rec.Code != http.StatusBadRequest {
		t.Errorf("malformed json: status = %d, want 400", rec.Code)
	}
	if rec := post(t, `{"spans":[]}`, "Bearer sekret"); rec.Code != http.StatusBadRequest {
		t.Errorf("missing install_id: status = %d, want 400", rec.Code)
	}
	real := `{"schema":"fusion.telemetry.v1","install_id":"http-test","spans":[{"agent":"claude","status":"success","duration_ms":42}]}`
	if rec := post(t, real, "Bearer sekret"); rec.Code != http.StatusAccepted {
		t.Errorf("valid payload: status = %d body = %q, want 202", rec.Code, rec.Body.String())
	}

	// The token guards reading what everyone sent, which is the side that
	// stays closed. Ingest auth is asserted nowhere on purpose: it is being
	// removed so clients can report with no configuration.
	summary := func(t *testing.T, auth string) *httptest.ResponseRecorder {
		t.Helper()
		req := httptest.NewRequest(http.MethodGet, "/v1/summary", nil)
		if auth != "" {
			req.Header.Set("Authorization", auth)
		}
		rec := httptest.NewRecorder()
		srv.handleSummary(rec, req)
		return rec
	}
	if rec := summary(t, ""); rec.Code != http.StatusUnauthorized {
		t.Errorf("summary without auth: status = %d, want 401", rec.Code)
	}
	if rec := summary(t, "Bearer wrong"); rec.Code != http.StatusUnauthorized {
		t.Errorf("summary with wrong token: status = %d, want 401", rec.Code)
	}
	getRec := summary(t, "Bearer sekret")
	if getRec.Code != http.StatusOK {
		t.Fatalf("summary: status = %d body = %q, want 200", getRec.Code, getRec.Body.String())
	}
	var resp summaryResponse
	if err := json.Unmarshal(getRec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode summary response: %v", err)
	}
	if resp.TotalSpans < 1 {
		t.Errorf("summary TotalSpans = %d, want at least 1 (the row just ingested over HTTP)", resp.TotalSpans)
	}
}
