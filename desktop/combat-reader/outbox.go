package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const collectorEndpoint = "https://eveosint.com/api/combat-repairs"

type outbox struct{ dir string }
type acknowledgement struct {
	ReportID string `json:"report_id"`
	SHA256   string `json:"sha256"`
	Status   string `json:"status"`
}

func directoryOf(path string) string { return filepath.Dir(path) }
func digest(body []byte) string      { sum := sha256.Sum256(body); return hex.EncodeToString(sum[:]) }
func writeDurable(path string, data []byte) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0700); err != nil {
		return err
	}
	f, err := os.CreateTemp(dir, ".pending-*")
	if err != nil {
		return err
	}
	tmp := f.Name()
	defer os.Remove(tmp)
	if _, err = f.Write(data); err != nil {
		f.Close()
		return err
	}
	if err = f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err = f.Close(); err != nil {
		return err
	}
	return durableRename(tmp, path)
}
func defaultOutbox() (*outbox, error) {
	root, err := os.UserCacheDir()
	if err != nil {
		return nil, err
	}
	return &outbox{filepath.Join(root, "EVEOSINT", "CombatReader", "outbox")}, nil
}
func (o *outbox) save(p CompactPayload) error {
	raw, err := p.JSON()
	if err != nil {
		return err
	}
	return writeDurable(filepath.Join(o.dir, digest([]byte(raw))+".json"), []byte(raw))
}
func validID(id string) bool {
	if len(id) != 64 {
		return false
	}
	_, err := hex.DecodeString(id)
	return err == nil && id == strings.ToLower(id)
}
func (o *outbox) pending() ([]string, error) {
	files, err := os.ReadDir(o.dir)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var ids []string
	for _, f := range files {
		id := strings.TrimSuffix(f.Name(), ".json")
		if f.IsDir() || !validID(id) {
			continue
		}
		ack, err := os.ReadFile(filepath.Join(o.dir, id+".ack.json"))
		var a acknowledgement
		if err == nil && json.Unmarshal(ack, &a) == nil && a.ReportID == id && a.SHA256 == id && a.Status == "stored" {
			continue
		}
		ids = append(ids, id)
	}
	return ids, nil
}
func (o *outbox) deliver(ctx context.Context, client *http.Client, endpoint, token, id string) error {
	if !validID(id) {
		return fmt.Errorf("identifiant de rapport invalide")
	}
	u, err := url.Parse(endpoint)
	if err != nil {
		return err
	}
	if u.Scheme != "https" && !(u.Scheme == "http" && (u.Hostname() == "127.0.0.1" || u.Hostname() == "::1")) {
		return fmt.Errorf("HTTPS requis")
	}
	body, err := os.ReadFile(filepath.Join(o.dir, id+".json"))
	if err != nil {
		return err
	}
	if len(body) > 65536 || digest(body) != id {
		return fmt.Errorf("rapport altéré ou trop grand, fichier conservé")
	}
	req, err := http.NewRequestWithContext(ctx, "POST", endpoint, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Idempotency-Key", id)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	// Never follow redirects, including redirects to login or another host.
	safe := *client
	safe.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	if safe.Timeout == 0 {
		safe.Timeout = 10 * time.Second
	}
	res, err := safe.Do(req)
	if err != nil {
		return fmt.Errorf("envoi non confirmé, fichier conservé : %w", err)
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return fmt.Errorf("HTTP %d, fichier conservé", res.StatusCode)
	}
	limited := io.LimitReader(res.Body, 4097)
	raw, err := io.ReadAll(limited)
	if err != nil {
		return err
	}
	if len(raw) > 4096 {
		return fmt.Errorf("accusé trop grand")
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	var ack acknowledgement
	if err = dec.Decode(&ack); err != nil {
		return fmt.Errorf("accusé illisible, fichier conservé")
	}
	if err = dec.Decode(new(any)); err != io.EOF {
		return fmt.Errorf("accusé invalide")
	}
	if ack.ReportID != id || ack.SHA256 != id || ack.Status != "stored" {
		return fmt.Errorf("accusé incohérent, fichier conservé")
	}
	// Preserve the original JSON even after acknowledgement. A durable receipt
	// prevents resending after restart; a crash before this write simply retries.
	return writeDurable(filepath.Join(o.dir, id+".ack.json"), raw)
}
func (o *outbox) run(ctx context.Context, token string, status chan<- string) {
	notify := func(s string) {
		select {
		case status <- s:
		default:
		}
	}
	ticker := time.NewTicker(15 * time.Second)
	defer ticker.Stop()
	for {
		ids, err := o.pending()
		if err != nil {
			notify("File d’attente inaccessible")
		} else {
			confirmed := 0
			for _, id := range ids {
				if ctx.Err() != nil {
					return
				}
				if err = o.deliver(ctx, &http.Client{Timeout: 10 * time.Second}, collectorEndpoint, token, id); err != nil {
					notify(err.Error())
					continue
				}
				confirmed++
			}
			if confirmed > 0 {
				notify(fmt.Sprintf("%d rapport(s) enregistré(s) et confirmé(s) par EVEOSINT", confirmed))
			} else if len(ids) == 0 {
				notify("Aucun rapport en attente")
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}
