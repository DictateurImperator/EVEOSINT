//go:build !windows

package main

import "os"

func openLog(path string) (*os.File, error) { return os.Open(path) }
