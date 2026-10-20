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
func jump(at, from, to string) Event {
	e := re("jump", at, 0)
	e.FromSystem = from
	e.ToSystem = to
	return e
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
func TestReceiptHourAndCharacterIsolation(t *testing.T) {
	events := []Event{re("shield", "18:59:59", 10), re("shield", "19:00:00", 20), re("armor", "19:59:59", 30), re("hull", "20:00:00", 40), re("shield", "20:00:01", 50)}
	other := re("shield", "19:30:00", 1000)
	other.Listener = "Other"
	events = append(events, other)
	r := mustReceipt(t, events, lossAt("20:00:00"))
	if amount(r) != 90 || len(r.Repairs) != 3 {
		t.Fatalf("%+v", r)
	}
}
func TestReceiptDistinctSystemsPerRepair(t *testing.T) {
	events := []Event{jump("19:00:00", "X", "A"), re("shield", "19:01:00", 10), jump("19:02:00", "A", "B"), re("shield", "19:03:00", 20), jump("19:04:00", "B", "A"), jump("19:05:00", "A", "B")}
	r := mustReceipt(t, events, lossAt("19:10:00"))
	if amount(r) != 30 {
		t.Fatalf("return to origin or repeated system discarded repairs: %+v", r)
	}
	events = append(events, jump("19:06:00", "B", "C"))
	r = mustReceipt(t, events, lossAt("19:10:00"))
	// A's repair saw B and C; B's repair saw A and C. Both expire.
	if amount(r) != 0 {
		t.Fatalf("two distinct other systems retained: %+v", r)
	}
	events = []Event{jump("19:00:00", "X", "A"), re("shield", "19:01:00", 10), jump("19:02:00", "A", "B"), re("shield", "19:03:00", 20), jump("19:04:00", "B", "C")}
	r = mustReceipt(t, events, lossAt("19:10:00"))
	if amount(r) != 20 {
		t.Fatalf("newer heal in B should survive: %+v", r)
	}
}
func TestReceiptDockAndPreviousLossBoundaries(t *testing.T) {
	for _, kind := range []string{"dock", "undock", "confirmed-loss", "ship-change"} {
		events := []Event{re("shield", "19:00:00", 100), re(kind, "19:10:00", 0), re("shield", "19:20:00", 40)}
		r := mustReceipt(t, events, lossAt("19:30:00"))
		if amount(r) != 40 {
			t.Fatalf("%s: %+v", kind, r)
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
func TestMovementParsesOnlyObservedMarkers(t *testing.T) {
	for _, line := range []string{"[ 2026.10.20 19:22:13 ] (notify) Setting course to docking perimeter", "[ 2026.10.20 19:22:13 ] (notify) Ship stopping", "[ 2026.10.20 19:22:13 ] (notify) Leave now or be destroyed.", "[ 2026.10.20 19:22:13 ] (notify) Ship is out of control"} {
		if _, ok := parseMovement(line, "Victim", "test.txt"); ok {
			t.Fatal("false lifecycle event", line)
		}
	}
	e, ok := parseMovement("[ 2026.10.20 18:06:06 ] (None) Undocking from Uemon VIII - Moon 10 - Zainou Biotech Production to Uemon solar system.", "Victim", "test.txt")
	if !ok || e.Kind != "undock" || e.ToSystem != "Uemon" {
		t.Fatalf("%+v", e)
	}
	e, ok = parseMovement("[ 2026.10.20 18:07:20 ] (None) Jumping from Paala to LXQ2-T", "Victim", "test.txt")
	if !ok || e.FromSystem != "Paala" || e.ToSystem != "LXQ2-T" {
		t.Fatalf("%+v", e)
	}
}
func TestReceiptUnknownOriginAndOutOfOrderInput(t *testing.T) {
	es := []Event{jump("19:04:00", "B", "C"), re("shield", "19:01:00", 100), jump("19:02:00", "A", "B")}
	r := mustReceipt(t, es, lossAt("19:10:00"))
	if amount(r) != 0 {
		t.Fatalf("origin inferred from next jump: %+v", r)
	}
}
func TestProvidedDockUndockFixture(t *testing.T) {
	p := os.Getenv("EVEOSINT_DOCK_LOG_FIXTURE")
	if p == "" {
		t.Skip("optional user-provided fixture")
	}
	b, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	events := []Event{}
	recentMoves := 0
	for _, l := range strings.Split(string(b), "\n") {
		if e, ok := parseLine(l, "Dictateur Imperator", "test.txt"); ok {
			events = append(events, e)
		}
		if e, ok := parseMovement(l, "Dictateur Imperator", "test.txt"); ok {
			events = append(events, e)
			if e.Time >= "2026.10.20 19:22:00" {
				recentMoves++
			}
		}
	}
	// Synthetic loss time: tests selection only, does NOT assert a real death.
	r, err := collectRepairs(events, Loss{"Dictateur Imperator", "Synthetic test ship", "2026.10.20 20:00:41"})
	if err != nil {
		t.Fatal(err)
	}
	if amount(r) != 68442 || len(r.Repairs) != 244 {
		t.Fatalf("%+v", r.Totals)
	}
	if recentMoves != 0 {
		t.Fatal("fixture changed: re-evaluate docking evidence")
	}
}
