package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

func candidate(at string) Event { return re("loss-candidate", at, 0) }
func testPacket(t *testing.T) CompactPayload {
	t.Helper()
	p, err := compactPayload(mustReceipt(t, []Event{re("shield", "19:00:00", 100)}, lossAt("19:01:00")), true)
	if err != nil {
		t.Fatal(err)
	}
	return p
}
func TestDeathSavesBeforeResetAndSuppressesRepeatedNotice(t *testing.T) {
	var s repairStore
	saved := 0
	s.save = func(p CompactPayload) error {
		saved++
		if p.Logi[0].Shield != 100 || p.Status != "pending_killmail" {
			t.Fatalf("%+v", p)
		}
		return nil
	}
	s.add(re("shield", "19:00:00", 100))
	if err := s.add(candidate("19:01:00")); err != nil {
		t.Fatal(err)
	}
	if len(s.pilots["Victim"].repairs) != 0 || saved != 1 {
		t.Fatal("not frozen/reset")
	}
	s.add(candidate("19:01:02"))
	if saved != 1 {
		t.Fatal("duplicate death")
	}
	s.add(re("shield", "19:02:00", 20))
	s.save = func(p CompactPayload) error {
		saved++
		if p.Logi[0].Shield != 20 {
			t.Fatal("old life carried over")
		}
		return nil
	}
	s.add(candidate("19:03:00"))
	if saved != 2 {
		t.Fatal("next life not saved")
	}
}
func TestSaveFailureNeverClearsCounterAndRetriesOriginal(t *testing.T) {
	var s repairStore
	s.save = func(CompactPayload) error { return errors.New("disk full") }
	s.add(re("shield", "19:00:00", 100))
	if err := s.add(candidate("19:01:00")); err == nil {
		t.Fatal("expected save failure")
	}
	if len(s.pilots["Victim"].repairs) != 1 {
		t.Fatal("lost unsaved repairs")
	}
	s.save = func(p CompactPayload) error {
		if p.LossTime != "2026-10-20T19:01:00Z" {
			t.Fatal("changed death time on retry")
		}
		return nil
	}
	if err := s.add(candidate("19:01:02")); err != nil {
		t.Fatal(err)
	}
	if len(s.pilots["Victim"].repairs) != 0 {
		t.Fatal("not reset after durable save")
	}
}
func TestOutboxAcknowledgementAndRestart(t *testing.T) {
	o := &outbox{t.TempDir()}
	p := testPacket(t)
	if err := o.save(p); err != nil {
		t.Fatal(err)
	}
	ids, _ := o.pending()
	if len(ids) != 1 {
		t.Fatal(ids)
	}
	id := ids[0]
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.Method != "POST" || r.Header.Get("Authorization") != "Bearer test-key" || r.Header.Get("Idempotency-Key") != id {
			t.Error("bad request")
		}
		json.NewEncoder(w).Encode(acknowledgement{id, id, "stored"})
	}))
	defer server.Close()
	if err := o.deliver(context.Background(), server.Client(), server.URL, "test-key", id); err != nil {
		t.Fatal(err)
	}
	restarted := &outbox{o.dir}
	pending, _ := restarted.pending()
	if len(pending) != 0 || calls != 1 {
		t.Fatal(pending, calls)
	}
	if _, err := os.Stat(filepath.Join(o.dir, id+".json")); err != nil {
		t.Fatal("original report not retained", err)
	}
}
func TestOutboxRejectsBadResponsesAndRetainsJSON(t *testing.T) {
	for _, mode := range []string{"401", "404", "500", "redirect", "html", "wrong-id", "trailing", "lost-ack"} {
		t.Run(mode, func(t *testing.T) {
			o := &outbox{t.TempDir()}
			o.save(testPacket(t))
			ids, _ := o.pending()
			id := ids[0]
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch mode {
				case "401":
					w.WriteHeader(401)
				case "404":
					w.WriteHeader(404)
				case "500":
					w.WriteHeader(500)
				case "redirect":
					w.Header().Set("Location", "/login")
					w.WriteHeader(302)
				case "html":
					fmt.Fprint(w, "<html>login</html>")
				case "wrong-id":
					json.NewEncoder(w).Encode(acknowledgement{"wrong", id, "stored"})
				case "trailing":
					json.NewEncoder(w).Encode(acknowledgement{id, id, "stored"})
					fmt.Fprint(w, "garbage")
				case "lost-ack":
					w.WriteHeader(200)
				}
			}))
			defer server.Close()
			if err := o.deliver(context.Background(), server.Client(), server.URL, "test", id); err == nil {
				t.Fatal("bad reply accepted")
			}
			left, _ := o.pending()
			if len(left) != 1 {
				t.Fatal("pending report lost")
			}
		})
	}
}
func TestTamperedOutboxNotSent(t *testing.T) {
	o := &outbox{t.TempDir()}
	o.save(testPacket(t))
	ids, _ := o.pending()
	id := ids[0]
	os.WriteFile(filepath.Join(o.dir, id+".json"), []byte("tampered"), 0600)
	called := false
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { called = true }))
	defer srv.Close()
	if err := o.deliver(context.Background(), srv.Client(), srv.URL, "key", id); err == nil || called {
		t.Fatal("tampered report transmitted")
	}
}
func TestRealDeathNoticeParser(t *testing.T) {
	e, ok := parseLine("[ 2026.10.17 23:34:44 ] (notify) Ship is out of control", "Victim", "x")
	if !ok || e.Kind != "loss-candidate" {
		t.Fatal(e)
	}
	if _, ok := parseLine("[ 2026.10.17 23:34:44 ] (notify) Enemy will be destroyed", "Victim", "x"); ok {
		t.Fatal("false death")
	}
}

func TestLiveCollectorIntegration(t *testing.T) {
	endpoint := os.Getenv("EVEOSINT_TEST_COLLECTOR_ENDPOINT")
	token := os.Getenv("EVEOSINT_TEST_COLLECTOR_TOKEN")
	if endpoint == "" || token == "" {
		t.Skip("isolated live API not running")
	}
	o := &outbox{t.TempDir()}
	if err := o.save(testPacket(t)); err != nil {
		t.Fatal(err)
	}
	ids, err := o.pending()
	if err != nil || len(ids) != 1 {
		t.Fatal(ids, err)
	}
	// Real POST twice, second one simulates a lost acknowledgement/retry.
	for n := 0; n < 2; n++ {
		if err = o.deliver(context.Background(), &http.Client{}, endpoint, token, ids[0]); err != nil {
			t.Fatal(err)
		}
	}
	remaining, err := o.pending()
	if err != nil || len(remaining) != 0 {
		t.Fatal(remaining, err)
	}
	t.Log("Real Go POST -> FastAPI -> PostgreSQL commit -> verified ACK, retry accepted")
}
