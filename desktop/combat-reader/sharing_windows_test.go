package main

import (
	"os"
	"path/filepath"
	"testing"
)

// Must run on Windows: proves an open reader permits append, another reader,
// rename and delete. Cross-compilation alone cannot prove OS sharing behavior.
func TestWindowsReaderDoesNotBlockWriterOrRotation(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "test.txt")
	if err := os.WriteFile(p, []byte(header), 0600); err != nil {
		t.Fatal(err)
	}
	reader, err := openLog(p)
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	appendLog(t, p, healing)
	other, err := openLog(p)
	if err != nil {
		t.Fatal(err)
	}
	other.Close()
	renamed := filepath.Join(dir, "renamed.txt")
	if err := os.Rename(p, renamed); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(renamed); err != nil {
		t.Fatal(err)
	}
}
