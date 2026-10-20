package main

import (
	"bytes"
	"context"
	"encoding/binary"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
	"unicode/utf16"
)

type Update struct {
	Event  *Event
	Status string
}
type cursor struct {
	offset                   int64
	pending                  []byte
	listener                 string
	encoding                 int // 0 UTF-8, 1 UTF-16 LE, 2 UTF-16 BE
	info                     os.FileInfo
	initialized, skipPartial bool
}
type watcher struct {
	dir    string
	files  map[string]*cursor
	replay bool
}

func newWatcher(dir string, replay bool) *watcher {
	return &watcher{dir: dir, files: make(map[string]*cursor), replay: replay}
}

func decode(b []byte, enc int) string {
	if enc == 0 {
		return strings.TrimPrefix(string(b), "\ufeff")
	}
	units := make([]uint16, len(b)/2)
	for i := range units {
		if enc == 1 {
			units[i] = binary.LittleEndian.Uint16(b[i*2:])
		} else {
			units[i] = binary.BigEndian.Uint16(b[i*2:])
		}
	}
	return strings.TrimPrefix(string(utf16.Decode(units)), "\ufeff")
}
func detect(b []byte) int {
	if bytes.HasPrefix(b, []byte{255, 254}) {
		return 1
	}
	if bytes.HasPrefix(b, []byte{254, 255}) {
		return 2
	}
	return 0
}
func listenerFrom(s string) string {
	for _, l := range strings.Split(s, "\n") {
		if strings.HasPrefix(strings.TrimSpace(l), "Listener:") {
			return strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(l), "Listener:"))
		}
	}
	return ""
}

// Poll reads at most 256 KiB per changed file. Every handle closes before updates
// are delivered. Incomplete writes are buffered until a whole newline arrives.
func (w *watcher) poll(ctx context.Context, emit func(Update)) error {
	entries, err := os.ReadDir(w.dir)
	if err != nil {
		return err
	}
	present := make(map[string]bool)
	blocked := make(map[string]bool)
	var batch []Event
	for _, entry := range entries {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if entry.IsDir() || entry.Type()&os.ModeSymlink != 0 || !strings.EqualFold(filepath.Ext(entry.Name()), ".txt") {
			continue
		}
		path := filepath.Join(w.dir, entry.Name())
		present[path] = true
		info, err := entry.Info()
		if err != nil {
			continue
		}
		c, exists := w.files[path]
		if !exists {
			c = &cursor{}
			w.files[path] = c
		}
		if c.initialized && c.info != nil && (!os.SameFile(c.info, info) || info.Size() < c.offset) {
			*c = cursor{}
		}
		if c.initialized && c.offset == info.Size() && c.info != nil && c.info.ModTime() == info.ModTime() {
			continue
		}
		f, err := openLog(path)
		if err != nil {
			emit(Update{Status: fmt.Sprintf("Lecture impossible (%s) : %v", entry.Name(), err)})
			continue
		}
		// Verify the opened object's identity, in case it changed since the listing.
		opened, err := f.Stat()
		if err != nil {
			f.Close()
			continue
		}
		if !opened.Mode().IsRegular() || (!c.initialized && opened.Size() < 2) {
			f.Close()
			continue
		}
		if c.initialized && c.info != nil && (!os.SameFile(c.info, opened) || opened.Size() < c.offset) {
			*c = cursor{}
		}
		if !c.initialized {
			header := make([]byte, 8192)
			n, _ := f.ReadAt(header, 0)
			header = header[:n]
			c.encoding = detect(header)
			c.listener = listenerFrom(decode(header, c.encoding))
			if !exists && !w.replay {
				c.offset = opened.Size()
				// A partial line already present at startup is not a new event.
				tail := make([]byte, 2)
				n, _ = f.ReadAt(tail, max(int64(0), c.offset-2))
				tail = tail[:n]
				if c.encoding == 0 {
					c.skipPartial = len(tail) > 0 && tail[len(tail)-1] != '\n'
				} else if c.encoding == 1 {
					c.skipPartial = !bytes.HasSuffix(tail, []byte{10, 0})
				} else {
					c.skipPartial = !bytes.HasSuffix(tail, []byte{0, 10})
				}
			}
			c.initialized = true
		}
		if blocked[c.listener] {
			f.Close()
			continue
		}
		_, err = f.Seek(c.offset, io.SeekStart)
		var chunk []byte
		if err == nil {
			chunk, err = io.ReadAll(io.LimitReader(f, 256*1024))
		}
		f.Close()
		if err != nil {
			emit(Update{Status: fmt.Sprintf("Erreur de lecture (%s) : %v", entry.Name(), err)})
			continue
		}
		c.offset += int64(len(chunk))
		c.info = opened
		if c.offset < opened.Size() {
			blocked[c.listener] = true
		}
		c.pending = append(c.pending, chunk...)
		newline := []byte{'\n'}
		if c.encoding == 1 {
			newline = []byte{10, 0}
		} else if c.encoding == 2 {
			newline = []byte{0, 10}
		}
		consumed := 0
		for {
			at := -1
			if c.encoding == 0 {
				if n := bytes.Index(c.pending[consumed:], newline); n >= 0 {
					at = consumed + n
				}
			} else {
				for n := consumed; n+1 < len(c.pending); n += 2 {
					if c.pending[n] == newline[0] && c.pending[n+1] == newline[1] {
						at = n
						break
					}
				}
			}
			if at < 0 {
				break
			}
			line := strings.TrimSuffix(decode(c.pending[consumed:at], c.encoding), "\r")
			consumed = at + len(newline)
			if c.skipPartial {
				c.skipPartial = false
				continue
			}
			if strings.HasPrefix(strings.TrimSpace(line), "Listener:") {
				c.listener = listenerFrom(line)
			}
			if e, ok := parseLine(line, c.listener, entry.Name()); ok {
				if e.Listener == "" {
					e.Listener = entry.Name()
				}
				batch = append(batch, e)
			}
		}
		c.pending = append([]byte(nil), c.pending[consumed:]...)
		if len(c.pending) > 1024*1024 {
			c.pending = nil
			c.skipPartial = true
			emit(Update{Status: "Ligne anormalement longue ignorée : " + entry.Name()})
		}
	}
	for p := range w.files {
		if !present[p] {
			delete(w.files, p)
		}
	}
	sort.SliceStable(batch, func(i, j int) bool { return batch[i].Time < batch[j].Time })
	for i := range batch {
		emit(Update{Event: &batch[i]})
	}
	// Only the initial directory snapshot skips historical contents. New files start at zero.
	w.replay = true
	return nil
}
func watch(ctx context.Context, dir string, replay bool, out chan<- Update) {
	defer close(out)
	emit := func(u Update) {
		select {
		case out <- u:
		case <-ctx.Done():
		}
	}
	w := newWatcher(dir, replay)
	timer := time.NewTicker(500 * time.Millisecond)
	defer timer.Stop()
	for {
		if err := w.poll(ctx, emit); err != nil {
			if ctx.Err() == nil {
				emit(Update{Status: fmt.Sprintf("Dossier inaccessible : %v", err)})
			}
			return
		}
		emit(Update{Status: fmt.Sprintf("Surveillance active — %d fichiers — lecture seule", len(w.files))})
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
		}
	}
}
