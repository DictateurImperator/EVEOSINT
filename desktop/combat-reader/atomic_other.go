//go:build !windows

package main

import "os"

func durableRename(from, to string) error {
	if err := os.Rename(from, to); err != nil {
		return err
	}
	d, err := os.OpenFile(directoryOf(to), os.O_RDONLY, 0)
	if err != nil {
		return err
	}
	defer d.Close()
	return d.Sync()
}
