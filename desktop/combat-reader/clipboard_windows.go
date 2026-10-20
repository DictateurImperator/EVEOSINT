package main

import (
	"fmt"
	"syscall"
	"unsafe"
)

// The clipboard is written only when the user presses Copy. No file is written.
func copyText(owner uintptr, text string) error {
	data, err := syscall.UTF16FromString(text)
	if err != nil {
		return err
	}
	h, _, _ := kernel32.NewProc("GlobalAlloc").Call(2, uintptr(len(data)*2))
	if h == 0 {
		return fmt.Errorf("allocation presse-papiers impossible")
	}
	owned := true
	defer func() {
		if owned {
			kernel32.NewProc("GlobalFree").Call(h)
		}
	}()
	ptr, _, _ := kernel32.NewProc("GlobalLock").Call(h)
	if ptr == 0 {
		return fmt.Errorf("accès mémoire presse-papiers impossible")
	}
	kernel32.NewProc("RtlMoveMemory").Call(ptr, uintptr(unsafe.Pointer(&data[0])), uintptr(len(data)*2))
	kernel32.NewProc("GlobalUnlock").Call(h)
	ok, _, _ := user32.NewProc("OpenClipboard").Call(owner)
	if ok == 0 {
		return fmt.Errorf("presse-papiers occupé, réessayez")
	}
	defer user32.NewProc("CloseClipboard").Call()
	ok, _, _ = user32.NewProc("EmptyClipboard").Call()
	if ok == 0 {
		return fmt.Errorf("presse-papiers indisponible")
	}
	ok, _, _ = user32.NewProc("SetClipboardData").Call(13, h)
	if ok == 0 {
		return fmt.Errorf("copie impossible")
	}
	owned = false // Windows owns the memory after SetClipboardData succeeds.
	return nil
}
