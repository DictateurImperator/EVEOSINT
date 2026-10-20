package main

import (
	"fmt"
	"syscall"
	"unsafe"
)

func durableRename(from, to string) error {
	a, err := syscall.UTF16PtrFromString(from)
	if err != nil {
		return err
	}
	b, err := syscall.UTF16PtrFromString(to)
	if err != nil {
		return err
	}
	ok, _, callErr := kernel32.NewProc("MoveFileExW").Call(uintptr(unsafe.Pointer(a)), uintptr(unsafe.Pointer(b)), 9)
	if ok == 0 {
		return fmt.Errorf("sauvegarde atomique : %w", callErr)
	}
	return nil
}
