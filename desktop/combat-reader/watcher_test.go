package main

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"unicode/utf16"
)

const header = "------------------------------------------------------------\n  Gamelog\n  Listener: Test Pilot\n  Session Started: 2026.10.20 17:46:39\n------------------------------------------------------------\n"
const healing = "[ 2026.10.20 19:22:13 ] (combat) <color=0xff00ff00>488</color> remote shield boosted by Scythe [ALLY] [CORP] [Test Logi] - - Medium S95a Scoped Remote Shield Booster\n"
const hit = "[ 2026.10.20 19:21:54 ] (combat) 52 from Test Attacker[CORP](Scythe) - Acolyte II - Hits\n"

func collect(t *testing.T, w *watcher) []Event {
	t.Helper()
	var out []Event
	err := w.poll(context.Background(), func(u Update) {
		if u.Event != nil {
			out = append(out, *u.Event)
		} else {
			t.Error(u.Status)
		}
	})
	if err != nil {
		t.Fatal(err)
	}
	return out
}
func appendLog(t *testing.T, p, s string) {
	t.Helper()
	f, e := os.OpenFile(p, os.O_WRONLY|os.O_APPEND, 0600)
	if e != nil {
		t.Fatal(e)
	}
	if _, e = f.WriteString(s); e != nil {
		t.Fatal(e)
	}
	if e = f.Close(); e != nil {
		t.Fatal(e)
	}
}
func TestRealShieldFormat(t *testing.T) {
	e, ok := parseLine(strings.TrimSpace(healing), "Test Pilot", "test.txt")
	if !ok || e.Actor != "Test Logi" || e.Amount != 488 || e.Kind != "shield" || e.Ship != "Scythe" {
		t.Fatalf("%+v %v", e, ok)
	}
}
func TestRejectNonCombat(t *testing.T) {
	for _, s := range []string{"[ 2026.10.20 19:22:13 ] (notify) https://private.invalid/secret", "Listener: Test Pilot", "[ 2026.10.20 19:22:13 ] (combat) Acolyte II belonging to Pilot misses you completely - Acolyte II"} {
		if _, ok := parseLine(s, "x", "x"); ok {
			t.Fatal(s)
		}
	}
}
func TestLivePartialAppendRotationAndReadOnly(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "old.txt")
	original := header + hit
	if err := os.WriteFile(p, []byte(original), 0600); err != nil {
		t.Fatal(err)
	}
	w := newWatcher(dir, false)
	if len(collect(t, w)) != 0 {
		t.Fatal("historical event replayed")
	}
	cut := len(healing) / 2
	appendLog(t, p, healing[:cut])
	if len(collect(t, w)) != 0 {
		t.Fatal("partial line emitted")
	}
	appendLog(t, p, healing[cut:])
	es := collect(t, w)
	if len(es) != 1 || es[0].Amount != 488 || es[0].Listener != "Test Pilot" {
		t.Fatalf("%+v", es)
	}
	if len(collect(t, w)) != 0 {
		t.Fatal("event replayed")
	}
	expected := sha256.Sum256([]byte(original + healing))
	actual, _ := os.ReadFile(p)
	if sha256.Sum256(actual) != expected {
		t.Fatal("log modified")
	}
	next := filepath.Join(dir, "next.txt")
	if e := os.WriteFile(next, []byte(header+healing), 0600); e != nil {
		t.Fatal(e)
	}
	if len(collect(t, w)) != 1 {
		t.Fatal("new session missed")
	}
	if e := os.Remove(p); e != nil {
		t.Fatal(e)
	}
	collect(t, w)
	if e := os.WriteFile(next, []byte(header+hit), 0600); e != nil {
		t.Fatal(e)
	}
	es = collect(t, w)
	if len(es) != 1 || es[0].Kind != "damage-in" {
		t.Fatalf("truncation: %+v", es)
	}
	entries, _ := os.ReadDir(dir)
	if len(entries) != 1 {
		t.Fatal("unexpected file created")
	}
}
func TestUTF16SplitAndZeroRepair(t *testing.T) {
	for _, big := range []bool{false, true} {
		dir := t.TempDir()
		p := filepath.Join(dir, "utf16.txt")
		var b []byte
		if big {
			b = []byte{254, 255}
		} else {
			b = []byte{255, 254}
		}
		text := header + strings.Replace(healing, "488", "0", 1)
		for _, u := range utf16.Encode([]rune(text)) {
			var x [2]byte
			if big {
				binary.BigEndian.PutUint16(x[:], u)
			} else {
				binary.LittleEndian.PutUint16(x[:], u)
			}
			b = append(b, x[:]...)
		}
		if e := os.WriteFile(p, b[:len(b)-1], 0600); e != nil {
			t.Fatal(e)
		}
		w := newWatcher(dir, true)
		if len(collect(t, w)) != 0 {
			t.Fatal("incomplete UTF16 emitted")
		}
		f, e := os.OpenFile(p, os.O_APPEND|os.O_WRONLY, 0600)
		if e != nil {
			t.Fatal(e)
		}
		_, e = f.Write(b[len(b)-1:])
		f.Close()
		if e != nil {
			t.Fatal(e)
		}
		es := collect(t, w)
		if len(es) != 1 || es[0].Amount != 0 || es[0].Actor != "Test Logi" {
			t.Fatalf("%+v", es)
		}
	}
}
func TestSkipPreexistingIncompleteLine(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "test.txt")
	os.WriteFile(p, []byte(header+healing[:40]), 0600)
	w := newWatcher(dir, false)
	collect(t, w)
	appendLog(t, p, healing[40:]+healing)
	es := collect(t, w)
	if len(es) != 1 {
		t.Fatalf("%+v", es)
	}
}
func TestReplacementSameName(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "test.txt")
	os.WriteFile(p, []byte(header+hit), 0600)
	w := newWatcher(dir, true)
	collect(t, w)
	os.Rename(p, filepath.Join(dir, "archive.old"))
	os.WriteFile(p, []byte(header+healing), 0600)
	es := collect(t, w)
	if len(es) != 1 || es[0].Kind != "shield" {
		t.Fatalf("%+v", es)
	}
}
func TestProvidedCombatFixture(t *testing.T) {
	path := os.Getenv("EVEOSINT_COMBAT_LOG_FIXTURE")
	if path == "" {
		t.Skip("optional user-provided fixture")
	}
	before, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var received int64
	var repairs, zero int
	c := listenerFrom(string(before))
	for _, l := range strings.Split(string(before), "\n") {
		e, ok := parseLine(l, c, filepath.Base(path))
		if ok && e.Kind == "shield" {
			received += e.Amount
			repairs++
			if e.Amount == 0 {
				zero++
			}
		}
	}
	if received != 1092 || repairs != 6 || zero != 3 {
		t.Fatalf("shield=%d events=%d zero=%d", received, repairs, zero)
	}
	after, _ := os.ReadFile(path)
	if sha256.Sum256(before) != sha256.Sum256(after) {
		t.Fatal("fixture changed")
	}
}
