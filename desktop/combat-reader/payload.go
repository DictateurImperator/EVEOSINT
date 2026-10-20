package main

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"
)

// V1 sends only aggregated received repairs, never raw log lines or file paths.
type LogiContribution struct {
	Pilot  string `json:"pilot"`
	Ship   string `json:"ship"`
	Shield int64  `json:"shield,omitempty"`
	Armor  int64  `json:"armor,omitempty"`
	Hull   int64  `json:"hull,omitempty"`
}
type Victim struct {
	Pilot string `json:"pilot"`
	Ship  string `json:"ship,omitempty"`
}
type CompactPayload struct {
	Status        string             `json:"status,omitempty"`
	Version       int                `json:"version"`
	Victim        Victim             `json:"victim"`
	LossTime      string             `json:"loss_time"`
	WindowSeconds int                `json:"window_seconds"`
	Partial       bool               `json:"partial"`
	Logi          []LogiContribution `json:"logi"`
}

func compactPayload(r Receipt, partial bool) (CompactPayload, error) {
	at, err := time.Parse(logTimeLayout, r.LossTime)
	if err != nil {
		return CompactPayload{}, err
	}
	if strings.TrimSpace(r.Pilot) == "" || strings.TrimSpace(r.Ship) == "" {
		return CompactPayload{}, fmt.Errorf("pilote et vaisseau de la perte requis")
	}
	p := CompactPayload{Version: 1, Victim: Victim{r.Pilot, r.Ship}, LossTime: at.Format(time.RFC3339), WindowSeconds: 1800, Partial: partial, Logi: []LogiContribution{}}
	grouped := map[[2]string]*LogiContribution{}
	// Derive from filtered repairs, not a potentially stale set of totals.
	for _, r := range r.Repairs {
		if r.Amount < 0 {
			return CompactPayload{}, fmt.Errorf("soins négatifs")
		}
		switch r.Kind {
		case "shield", "armor", "hull":
		default:
			return CompactPayload{}, fmt.Errorf("type de soin inconnu")
		}
		if r.Amount == 0 {
			continue
		}
		if r.Pilot == "" || r.Ship == "" {
			return CompactPayload{}, fmt.Errorf("source du soin incomplète")
		}
		key := [2]string{r.Pilot, r.Ship}
		v := grouped[key]
		if v == nil {
			v = &LogiContribution{Pilot: r.Pilot, Ship: r.Ship}
			grouped[key] = v
		}
		switch r.Kind {
		case "shield":
			v.Shield += r.Amount
		case "armor":
			v.Armor += r.Amount
		case "hull":
			v.Hull += r.Amount
		}
	}
	for _, v := range grouped {
		p.Logi = append(p.Logi, *v)
	}
	sort.Slice(p.Logi, func(i, j int) bool {
		if p.Logi[i].Pilot != p.Logi[j].Pilot {
			return p.Logi[i].Pilot < p.Logi[j].Pilot
		}
		return p.Logi[i].Ship < p.Logi[j].Ship
	})
	return p, nil
}
func (p CompactPayload) JSON() (string, error) { b, err := json.Marshal(p); return string(b), err }
func (p CompactPayload) Summary() string {
	ship := p.Victim.Ship
	if ship == "" {
		ship = "vaisseau à confirmer"
	}
	rows := []string{fmt.Sprintf("%s — %s | Signal : %s | Fenêtre : 30 minutes", p.Victim.Pilot, ship, p.LossTime)}
	if p.Status == "pending_killmail" {
		rows = append(rows, "Rapport candidat : heure exacte et vaisseau à confirmer par killmail.")
	}
	if p.Partial {
		rows = append(rows, "Couverture partielle : la session ne couvre pas toute la fenêtre demandée.")
	}
	rows = append(rows, "Pilote logi | Vaisseau | Shield | Armor | Structure")
	for _, v := range p.Logi {
		rows = append(rows, fmt.Sprintf("%s | %s | %d | %d | %d", v.Pilot, v.Ship, v.Shield, v.Armor, v.Hull))
	}
	if len(p.Logi) == 0 {
		rows = append(rows, "Aucun soin positif trouvé dans les données disponibles.")
	}
	return strings.Join(rows, "\r\n")
}

// Memory-only rolling windows. Input may arrive out of order when several log
// files are replayed; only the newest 30 minutes per character are retained.
type repairWindow struct {
	first, latest time.Time
	repairs       []Event
	lastDeath     time.Time
	armed         bool
	unsaved       *CompactPayload
	lastReport    *CompactPayload
}
type repairStore struct {
	pilots map[string]*repairWindow
	save   func(CompactPayload) error
}

func (s *repairStore) add(e Event) error {
	at, err := time.Parse(logTimeLayout, e.Time)
	if err != nil {
		return err
	}
	if s.pilots == nil {
		s.pilots = map[string]*repairWindow{}
	}
	w := s.pilots[e.Listener]
	if w == nil {
		w = &repairWindow{first: at, latest: at, armed: true}
		s.pilots[e.Listener] = w
	}
	if w.unsaved != nil {
		if err := s.savePending(w); err != nil {
			return err
		}
	}
	if e.Kind == "loss-candidate" {
		if !w.armed || (!w.lastDeath.IsZero() && !at.After(w.lastDeath)) {
			return nil
		}
		if at.Before(w.latest) {
			return fmt.Errorf("perte reçue dans le désordre : rapport non réinitialisé")
		}
		r, err := collectRepairs(w.repairs, Loss{e.Listener, "non déterminé", e.Time})
		if err != nil {
			return err
		}
		p, err := compactPayload(r, w.first.After(at.Add(-30*time.Minute)))
		if err != nil {
			return err
		}
		p.Victim.Ship = ""
		p.Status = "pending_killmail"
		w.unsaved = &p
		return s.savePending(w)
	}
	if !w.lastDeath.IsZero() && !at.After(w.lastDeath) {
		return nil
	}
	if isRepair(e) || e.Kind == "damage-in" {
		w.armed = true
	}
	if at.Before(w.first) {
		w.first = at
	}
	if at.After(w.latest) {
		w.latest = at
	}
	start := w.latest.Add(-30 * time.Minute)
	if isRepair(e) && !at.Before(start) {
		// Retain only fields needed for the final calculation, in memory.
		w.repairs = append(w.repairs, Event{Time: e.Time, Listener: e.Listener, Kind: e.Kind, Actor: e.Actor, Ship: e.Ship, Amount: e.Amount})
	}
	kept := w.repairs[:0]
	for _, r := range w.repairs {
		t, _ := time.Parse(logTimeLayout, r.Time)
		if !t.Before(start) {
			kept = append(kept, r)
		}
	}
	for i := len(kept); i < len(w.repairs); i++ {
		w.repairs[i] = Event{}
	}
	w.repairs = kept
	return nil
}

// The old counter is cleared only after the frozen JSON is durably saved.
func (s *repairStore) savePending(w *repairWindow) error {
	if s.save == nil {
		return fmt.Errorf("sauvegarde des pertes indisponible : compteur conservé")
	}
	if err := s.save(*w.unsaved); err != nil {
		return fmt.Errorf("sauvegarde impossible, compteur conservé : %w", err)
	}
	at, err := time.Parse(time.RFC3339, w.unsaved.LossTime)
	if err != nil {
		return err
	}
	w.lastReport = w.unsaved
	w.unsaved = nil
	w.repairs = nil
	w.lastDeath = at
	w.first = at
	w.latest = at
	w.armed = false
	return nil
}

func (s *repairStore) prepare(loss Loss) (CompactPayload, error) {
	w := s.pilots[loss.Pilot]
	if w == nil {
		return CompactPayload{}, fmt.Errorf("aucun événement lu pour ce personnage")
	}
	at, err := time.Parse(logTimeLayout, loss.Time)
	if err != nil {
		return CompactPayload{}, fmt.Errorf("heure attendue : AAAA.MM.JJ HH:MM:SS (UTC)")
	}
	r, err := collectRepairs(w.repairs, loss)
	if err != nil {
		return CompactPayload{}, err
	}
	start := at.Add(-30 * time.Minute)
	partial := w.first.After(start) || w.latest.Before(at) || w.latest.Add(-30*time.Minute).After(start)
	return compactPayload(r, partial)
}

func (s *repairStore) liveSummary() string {
	pilots := make([]string, 0, len(s.pilots))
	for p := range s.pilots {
		pilots = append(pilots, p)
	}
	sort.Strings(pilots)
	rows := []string{"Personnage soigné | Pilote logi | Vaisseau logi | Shield | Armor | Structure"}
	for _, pilot := range pilots {
		w := s.pilots[pilot]
		r, err := collectRepairs(w.repairs, Loss{pilot, "suivi local", w.latest.Format(logTimeLayout)})
		if err != nil {
			continue
		}
		p, err := compactPayload(r, true)
		if err != nil {
			continue
		}
		for _, v := range p.Logi {
			rows = append(rows, fmt.Sprintf("%s | %s | %s | %d | %d | %d", pilot, v.Pilot, v.Ship, v.Shield, v.Armor, v.Hull))
		}
	}
	if len(rows) == 1 {
		rows = append(rows, "Aucun soin positif dans la fenêtre disponible.")
	}
	return strings.Join(rows, "\r\n")
}
