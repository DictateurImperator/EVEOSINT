package main

import (
	"fmt"
	"sort"
	"time"
)

const logTimeLayout = "2006.01.02 15:04:05"

// Loss must come from a confirmed loss/killmail. A combat notification or an
// arbitrary occurrence of "destroyed" must never be promoted to a confirmed loss.
type Loss struct{ Pilot, Ship, Time string }
type Repair struct {
	Time   string `json:"time"`
	Pilot  string `json:"pilot"`
	Ship   string `json:"ship"`
	Kind   string `json:"kind"`
	Amount int64  `json:"amount"`
}
type RepairTotal struct {
	Pilot  string `json:"pilot"`
	Ship   string `json:"ship"`
	Kind   string `json:"kind"`
	Amount int64  `json:"amount"`
	Events int    `json:"events"`
}

// Receipt is intentionally separate from Event: no local paths, source filenames,
// movements, invitations, damage events or module descriptions enter this payload.
// There is no network transport in this version.
type Receipt struct {
	Pilot       string        `json:"pilot"`
	Ship        string        `json:"ship"`
	LossTime    string        `json:"loss_time"`
	WindowStart string        `json:"window_start"`
	Repairs     []Repair      `json:"repairs"`
	Totals      []RepairTotal `json:"totals"`
}

func isRepair(e Event) bool { return e.Kind == "shield" || e.Kind == "armor" || e.Kind == "hull" }

// collectRepairs includes only this pilot's received repairs in the fixed,
// inclusive interval [confirmed loss - 30 minutes, confirmed loss]. No docking,
// movement or other lifecycle event changes this window.
func collectRepairs(events []Event, loss Loss) (Receipt, error) {
	death, err := time.Parse(logTimeLayout, loss.Time)
	if err != nil {
		return Receipt{}, fmt.Errorf("invalid loss time: %w", err)
	}
	if loss.Pilot == "" || loss.Ship == "" {
		return Receipt{}, fmt.Errorf("confirmed victim pilot and ship required")
	}
	start := death.Add(-30 * time.Minute)
	out := Receipt{Pilot: loss.Pilot, Ship: loss.Ship, LossTime: loss.Time, WindowStart: start.Format(logTimeLayout), Repairs: []Repair{}, Totals: []RepairTotal{}}
	type timedEvent struct {
		event Event
		at    time.Time
	}
	ordered := make([]timedEvent, 0, len(events))
	for _, e := range events {
		if e.Listener != loss.Pilot || !isRepair(e) {
			continue
		}
		at, err := time.Parse(logTimeLayout, e.Time)
		if err != nil {
			return Receipt{}, fmt.Errorf("invalid event time: %w", err)
		}
		if !at.Before(start) && !at.After(death) {
			ordered = append(ordered, timedEvent{e, at})
		}
	}
	sort.SliceStable(ordered, func(i, j int) bool { return ordered[i].at.Before(ordered[j].at) })
	totals := map[string]*RepairTotal{}
	for _, item := range ordered {
		e := item.event
		if e.Amount < 0 {
			return Receipt{}, fmt.Errorf("negative repair amount")
		}
		r := Repair{e.Time, e.Actor, e.Ship, e.Kind, e.Amount}
		out.Repairs = append(out.Repairs, r)
		key := r.Pilot + "\x00" + r.Ship + "\x00" + r.Kind
		t := totals[key]
		if t == nil {
			t = &RepairTotal{Pilot: r.Pilot, Ship: r.Ship, Kind: r.Kind}
			totals[key] = t
		}
		t.Amount += r.Amount
		t.Events++
	}
	for _, t := range totals {
		out.Totals = append(out.Totals, *t)
	}
	sort.Slice(out.Totals, func(i, j int) bool {
		a, b := out.Totals[i], out.Totals[j]
		if a.Pilot != b.Pilot {
			return a.Pilot < b.Pilot
		}
		if a.Ship != b.Ship {
			return a.Ship < b.Ship
		}
		return a.Kind < b.Kind
	})
	return out, nil
}
