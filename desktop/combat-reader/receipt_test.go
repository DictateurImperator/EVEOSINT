package main

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

func re(kind, at string, amount int64) Event {
	return Event{Listener: "Victim", Kind: kind, Time: "2026.10.20 " + at, Amount: amount, Actor: "Logi", Ship: "Scythe", Source: "secret-local-path.txt", Module: "private module"}
}
func lossAt(at string) Loss { return Loss{"Victim", "Test Ship", "2026.10.20 " + at} }
func amount(r Receipt) int64 {
	var total int64
	for _, e := range r.Repairs {
		total += e.Amount
	}
	return total
}
func mustReceipt(t *testing.T, events []Event, loss Loss) Receipt {
	t.Helper()
	r, err := collectRepairs(events, loss)
	if err != nil {
		t.Fatal(err)
	}
	return r
}
func TestReceiptThirtyMinutesAndCharacterIsolation(t *testing.T) {
	events := []Event{re("shield", "19:29:59", 10), re("shield", "19:30:00", 20), re("armor", "19:59:59", 30), re("hull", "20:00:00", 40), re("shield", "20:00:01", 50)}
	other := re("shield", "19:30:00", 1000)
	other.Listener = "Other"
	events = append(events, other)
	r := mustReceipt(t, events, lossAt("20:00:00"))
	if amount(r) != 90 || len(r.Repairs) != 3 || r.WindowStart != "2026.10.20 19:30:00" {
		t.Fatalf("%+v", r)
	}
}
func TestReceiptLifecycleNeverCutsThirtyMinuteWindow(t *testing.T) {
	for _, kind := range []string{"dock", "undock", "jump", "confirmed-loss", "ship-change"} {
		events := []Event{re("shield", "19:01:00", 100), re(kind, "19:10:00", 0), re(kind, "19:11:00", 0), re("shield", "19:20:00", 40)}
		r := mustReceipt(t, events, lossAt("19:30:00"))
		if amount(r) != 140 {
			t.Fatalf("%s changed the fixed window: %+v", kind, r)
		}
	}
}
func TestReceiptZeroAndGroupingAndMinimalPayload(t *testing.T) {
	es := []Event{re("shield", "19:00:00", 488), re("shield", "19:00:01", 0), re("shield", "19:00:02", 487), re("shield", "19:00:03", 117)}
	r := mustReceipt(t, es, lossAt("19:01:00"))
	if amount(r) != 1092 || len(r.Totals) != 1 || r.Totals[0].Events != 4 {
		t.Fatalf("%+v", r)
	}
	b, _ := json.Marshal(r)
	for _, secret := range []string{"secret-local-path", "private module", "from_system", "to_system", "source"} {
		if strings.Contains(string(b), secret) {
			t.Fatal("nonessential data leaked:", string(b))
		}
	}
}
func TestReceiptRejectsUnconfirmedIdentityAndMalformedTime(t *testing.T) {
	for _, l := range []Loss{{"", "Ship", "2026.10.20 20:00:00"}, {"Pilot", "", "2026.10.20 20:00:00"}, {"Pilot", "Ship", "invalid"}} {
		if _, err := collectRepairs(nil, l); err == nil {
			t.Fatal("accepted incomplete loss")
		}
	}
}
func TestReceiptOutOfOrderInput(t *testing.T) {
	es := []Event{re("shield", "19:29:00", 20), re("shield", "19:01:00", 100)}
	r := mustReceipt(t, es, lossAt("19:30:00"))
	if amount(r) != 120 || r.Repairs[0].Time != "2026.10.20 19:01:00" {
		t.Fatalf("%+v", r)
	}
}
func TestProvidedThirtyMinuteFixture(t *testing.T) {
	p := os.Getenv("EVEOSINT_DOCK_LOG_FIXTURE")
	if p == "" {
		t.Skip("optional user-provided fixture")
	}
	b, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	events := []Event{}
	for _, l := range strings.Split(string(b), "\n") {
		if e, ok := parseLine(l, "Dictateur Imperator", "test.txt"); ok {
			events = append(events, e)
		}
	}
	// Synthetic loss time: tests selection only, does NOT assert a real death.
	r, err := collectRepairs(events, Loss{"Dictateur Imperator", "Synthetic test ship", "2026.10.20 20:00:41"})
	if err != nil {
		t.Fatal(err)
	}
	if amount(r) != 67350 || len(r.Repairs) != 238 {
		t.Fatalf("%+v", r.Totals)
	}
}
