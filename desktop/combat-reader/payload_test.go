package main

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

func TestCompactPayloadGroupsPilotShipAndTypes(t *testing.T) {
	es := []Event{re("shield", "19:00:00", 488), re("shield", "19:00:01", 487), re("shield", "19:00:02", 117), re("armor", "19:00:03", 40), re("hull", "19:00:04", 15), re("shield", "19:00:05", 0)}
	other := re("shield", "19:00:06", 20)
	other.Ship = "Basilisk"
	es = append(es, other)
	r := mustReceipt(t, es, lossAt("19:01:00"))
	p, err := compactPayload(r, false)
	if err != nil {
		t.Fatal(err)
	}
	if len(p.Logi) != 2 || p.Logi[1].Shield != 1092 || p.Logi[1].Armor != 40 || p.Logi[1].Hull != 15 || p.WindowSeconds != 1800 || p.LossTime != "2026-10-20T19:01:00Z" {
		t.Fatalf("%+v", p)
	}
	raw, err := p.JSON()
	if err != nil {
		t.Fatal(err)
	}
	for _, excluded := range []string{"secret-local-path", "private module", "module", "source", "repairs", "events", "19:00:01"} {
		if strings.Contains(raw, excluded) {
			t.Fatal("unnecessary data in payload", raw)
		}
	}
	var roundTrip CompactPayload
	if err = json.Unmarshal([]byte(raw), &roundTrip); err != nil {
		t.Fatal(err)
	}
	again, _ := roundTrip.JSON()
	if raw != again {
		t.Fatal("unstable payload")
	}
	if strings.Contains(raw, "\n") {
		t.Fatal("expected single-line JSON")
	}
}
func TestCompactPayloadOmitsZeroOnlyContributors(t *testing.T) {
	r := mustReceipt(t, []Event{re("shield", "19:00:00", 0)}, lossAt("19:01:00"))
	p, e := compactPayload(r, true)
	if e != nil {
		t.Fatal(e)
	}
	raw, _ := p.JSON()
	if len(p.Logi) != 0 || !strings.Contains(raw, `"logi":[]`) {
		t.Fatal(raw)
	}
}
func TestRepairStoreThirtyMinutesAndPartialCoverage(t *testing.T) {
	var s repairStore
	for _, e := range []Event{re("damage-in", "19:00:00", 5), re("shield", "19:00:00", 100), re("shield", "19:00:01", 50), re("armor", "19:30:00", 10)} {
		if err := s.add(e); err != nil {
			t.Fatal(err)
		}
	}
	p, err := s.prepare(lossAt("19:30:00"))
	if err != nil {
		t.Fatal(err)
	}
	if p.Partial || p.Logi[0].Shield != 150 {
		t.Fatalf("%+v", p)
	}
	if err = s.add(re("damage-in", "19:30:01", 10)); err != nil {
		t.Fatal(err)
	}
	p, err = s.prepare(lossAt("19:30:01"))
	if err != nil {
		t.Fatal(err)
	}
	if p.Logi[0].Shield != 50 || len(s.pilots["Victim"].repairs) != 2 {
		t.Fatalf("retention: %+v", p)
	}
	p, err = s.prepare(lossAt("19:30:00"))
	if err != nil {
		t.Fatal(err)
	}
	if !p.Partial {
		t.Fatal("earlier report must disclose pruned data")
	}
	p, err = s.prepare(lossAt("19:30:30"))
	if err != nil {
		t.Fatal(err)
	}
	if !p.Partial {
		t.Fatal("missing final part must be disclosed")
	}
	for _, e := range s.pilots["Victim"].repairs {
		if e.Source != "" || e.Module != "" {
			t.Fatal("unnecessary data retained")
		}
	}
}
func TestRepairStoreIsolatesCharactersAndLateInput(t *testing.T) {
	var s repairStore
	s.add(re("shield", "19:00:00", 100))
	other := re("shield", "20:00:00", 900)
	other.Listener = "Other"
	s.add(other)
	p, e := s.prepare(lossAt("19:00:00"))
	if e != nil || p.Logi[0].Shield != 100 {
		t.Fatalf("%+v %v", p, e)
	}
	s.add(re("shield", "19:00:02", 20))
	s.add(re("shield", "19:00:01", 10))
	p, e = s.prepare(lossAt("19:00:02"))
	if e != nil || p.Logi[0].Shield != 130 {
		t.Fatalf("%+v %v", p, e)
	}
	if _, e = s.prepare(Loss{"Unknown", "Ship", "2026.10.20 20:00:00"}); e == nil {
		t.Fatal("unknown character accepted")
	}
}
func TestOutgoingRepairDoesNotDuplicateReceivedRepair(t *testing.T) {
	line := "[ 2026.10.20 19:22:13 ] (combat) 488 remote shield boosted to Nereus [CONDI] [SNOOO] [Dictateur Imperator] - - Medium S95a Scoped Remote Shield Booster"
	if e, ok := parseLine(line, "Yunan", "test.txt"); ok && isRepair(e) {
		t.Fatal("outgoing repair would double count")
	}
}
func TestCompactJSONEscapesNames(t *testing.T) {
	e := re("shield", "19:00:00", 10)
	e.Actor = "Pilote \"été\""
	r := mustReceipt(t, []Event{e}, lossAt("19:01:00"))
	p, err := compactPayload(r, true)
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := p.JSON()
	var parsed CompactPayload
	if err = json.Unmarshal([]byte(raw), &parsed); err != nil {
		t.Fatal(err)
	}
	if parsed.Logi[0].Pilot != e.Actor {
		t.Fatal(raw)
	}
}

func TestProvidedCompactFixture(t *testing.T) {
	path := os.Getenv("EVEOSINT_COMPACT_LOG_FIXTURE")
	if path == "" {
		t.Skip("optional user-provided fixture")
	}
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var store repairStore
	for _, line := range strings.Split(string(b), "\n") {
		if e, ok := parseLine(line, "Dictateur Imperator", path); ok {
			if err := store.add(e); err != nil {
				t.Fatal(err)
			}
		}
	}
	// Synthetic loss time used solely to exercise the 30-minute export window.
	p, err := store.prepare(Loss{"Dictateur Imperator", "Nereus", "2026.10.20 20:03:34"})
	if err != nil {
		t.Fatal(err)
	}
	if len(p.Logi) != 1 || p.Logi[0].Pilot != "Yunan" || p.Logi[0].Ship != "Scythe" || p.Logi[0].Shield != 79829 {
		t.Fatalf("%+v", p)
	}
	raw, err := p.JSON()
	if err != nil {
		t.Fatal(err)
	}
	if len(raw) >= 512 || strings.Contains(raw, path) || strings.Contains(raw, "Medium S95a") {
		t.Fatal("payload is not minimal")
	}
	t.Logf("Real log: %d bytes; aggregated JSON: %d bytes (synthetic loss)", len(b), len(raw))
}
