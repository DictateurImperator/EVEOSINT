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
type retainedRepair struct {
	repair  Repair
	origin  string
	visited map[string]bool
}

func isRepair(e Event) bool { return e.Kind == "shield" || e.Kind == "armor" || e.Kind == "hull" }

// collectRepairs evaluates each repair independently. Two DISTINCT other systems
// visited AFTER a repair expire that repair. Returning to its origin doesn't
// count: A -> B -> A retains it; A -> B -> C expires it. Repairs in B have their
// own origin B, and therefore remain eligible when reaching C.
// Docking/undocking or a confirmed previous loss separates ship lives. Undocking
// is a known marker proving a station visit; it is not an immediate docking signal.
func collectRepairs(events []Event, loss Loss) (Receipt, error) {
	death, err := time.Parse(logTimeLayout, loss.Time)
	if err != nil {
		return Receipt{}, fmt.Errorf("invalid loss time: %w", err)
	}
	if loss.Pilot == "" || loss.Ship == "" {
		return Receipt{}, fmt.Errorf("confirmed victim pilot and ship required")
	}
	start := death.Add(-time.Hour)
	out := Receipt{Pilot: loss.Pilot, Ship: loss.Ship, LossTime: loss.Time, WindowStart: start.Format(logTimeLayout), Repairs: []Repair{}, Totals: []RepairTotal{}}
	type timedEvent struct {
		event Event
		at    time.Time
	}
	ordered := make([]timedEvent, 0, len(events))
	for _, e := range events {
		if e.Listener != loss.Pilot {
			continue
		}
		at, err := time.Parse(logTimeLayout, e.Time)
		if err != nil {
			return Receipt{}, fmt.Errorf("invalid event time: %w", err)
		}
		if !at.After(death) {
			ordered = append(ordered, timedEvent{e, at})
		}
	}
	sort.SliceStable(ordered, func(i, j int) bool { return ordered[i].at.Before(ordered[j].at) })
	current := ""
	pending := []retainedRepair{}
	for _, item := range ordered {
		e := item.event
		switch e.Kind {
		case "jump":
			if e.FromSystem == "" || e.ToSystem == "" || e.FromSystem == e.ToSystem {
				continue
			}
			current = e.ToSystem
			kept := pending[:0]
			for _, r := range pending {
				if r.origin == "" {
					r.origin = e.FromSystem
				}
				if e.ToSystem != r.origin {
					r.visited[e.ToSystem] = true
				}
				if len(r.visited) < 2 {
					kept = append(kept, r)
				}
			}
			pending = kept
		case "dock", "undock", "confirmed-loss", "ship-change":
			pending = nil
			if e.Kind == "undock" {
				current = e.ToSystem
			}
		default:
			if isRepair(e) && !item.at.Before(start) {
				if e.Amount < 0 {
					return Receipt{}, fmt.Errorf("negative repair amount")
				}
				pending = append(pending, retainedRepair{Repair{e.Time, e.Actor, e.Ship, e.Kind, e.Amount}, current, make(map[string]bool)})
			}
		}
	}
	totals := map[string]*RepairTotal{}
	for _, r := range pending {
		out.Repairs = append(out.Repairs, r.repair)
		key := r.repair.Pilot + "\x00" + r.repair.Ship + "\x00" + r.repair.Kind
		t := totals[key]
		if t == nil {
			t = &RepairTotal{Pilot: r.repair.Pilot, Ship: r.repair.Ship, Kind: r.repair.Kind}
			totals[key] = t
		}
		t.Amount += r.repair.Amount
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
