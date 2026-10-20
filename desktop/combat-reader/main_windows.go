package main

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"syscall"
	"unsafe"
)

var user32 = syscall.NewLazyDLL("user32.dll")
var kernel32 = syscall.NewLazyDLL("kernel32.dll")
var shell32 = syscall.NewLazyDLL("shell32.dll")
var createWindow = user32.NewProc("CreateWindowExW")
var send = user32.NewProc("SendMessageW")
var setText = user32.NewProc("SetWindowTextW")
var getText = user32.NewProc("GetWindowTextW")
var move = user32.NewProc("MoveWindow")
var controls []uintptr
var pathEdit, replayBox, startButton, stopButton, statusText, summaryEdit, eventsEdit uintptr
var updates <-chan Update
var cancel context.CancelFunc
var history []string
var totals = map[string]int64{}
var count int
var lastError string

type point struct{ X, Y int32 }
type message struct {
	Hwnd    uintptr
	Msg     uint32
	WParam  uintptr
	LParam  uintptr
	Time    uint32
	Pt      point
	Private uint32
}
type windowClass struct {
	Size, Style                        uint32
	Proc                               uintptr
	ClsExtra, WndExtra                 int32
	Instance, Icon, Cursor, Background uintptr
	Menu, Name                         *uint16
	IconSmall                          uintptr
}

func wide(s string) *uint16 { return syscall.StringToUTF16Ptr(s) }
func textOf(h uintptr) string {
	n, _, _ := user32.NewProc("GetWindowTextLengthW").Call(h)
	b := make([]uint16, n+1)
	getText.Call(h, uintptr(unsafe.Pointer(&b[0])), uintptr(len(b)))
	return syscall.UTF16ToString(b)
}
func put(h uintptr, s string) { setText.Call(h, uintptr(unsafe.Pointer(wide(s)))) }
func child(parent uintptr, class, title string, style, id uintptr) uintptr {
	h, _, _ := createWindow.Call(0, uintptr(unsafe.Pointer(wide(class))), uintptr(unsafe.Pointer(wide(title))), 0x50000000|style, 0, 0, 10, 10, parent, id, 0, 0)
	font, _, _ := syscall.NewLazyDLL("gdi32.dll").NewProc("GetStockObject").Call(17)
	send.Call(h, 0x30, font, 1)
	controls = append(controls, h)
	return h
}

type browseInfo struct {
	Owner, Root     uintptr
	Display, Title  *uint16
	Flags           uint32
	Callback, Param uintptr
	Image           int32
}

func browse(owner uintptr) {
	if cancel != nil {
		return
	}
	display := make([]uint16, 32768)
	bi := browseInfo{Owner: owner, Display: &display[0], Title: wide("Choisissez le dossier EVE\\logs\\Gamelogs"), Flags: 0x41}
	pidl, _, _ := shell32.NewProc("SHBrowseForFolderW").Call(uintptr(unsafe.Pointer(&bi)))
	if pidl == 0 {
		return
	}
	defer syscall.NewLazyDLL("ole32.dll").NewProc("CoTaskMemFree").Call(pidl)
	b := make([]uint16, 32768)
	ok, _, _ := shell32.NewProc("SHGetPathFromIDListW").Call(pidl, uintptr(unsafe.Pointer(&b[0])))
	if ok != 0 {
		put(pathEdit, syscall.UTF16ToString(b))
	}
}
func defaultFolder() string {
	// CSIDL_PERSONAL resolves redirected Documents, including OneDrive.
	b := make([]uint16, 32768)
	r, _, _ := shell32.NewProc("SHGetFolderPathW").Call(0, 5, 0, 0, uintptr(unsafe.Pointer(&b[0])))
	if r == 0 {
		return filepath.Join(syscall.UTF16ToString(b), "EVE", "logs", "Gamelogs")
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, "Documents", "EVE", "logs", "Gamelogs")
}
func layout(w, h int32) {
	positions := [][4]int32{{16, 12, w - 32, 24}, {16, 40, w - 170, 28}, {w - 144, 40, 128, 28}, {16, 78, w - 32, 24}, {16, 110, w - 32, 26}, {16, 145, 130, 30}, {158, 145, 130, 30}, {16, 188, w - 32, 28}, {16, 220, w - 32, 125}, {16, 355, w - 32, 24}, {16, 382, w - 32, max(int32(50), h-398)}}
	for i, p := range positions {
		move.Call(controls[i], uintptr(p[0]), uintptr(p[1]), uintptr(max(int32(1), p[2])), uintptr(p[3]), 1)
	}
}
func stop() {
	if cancel != nil {
		cancel()
		cancel = nil
	}
	user32.NewProc("EnableWindow").Call(startButton, 1)
	user32.NewProc("EnableWindow").Call(pathEdit, 1)
	user32.NewProc("EnableWindow").Call(replayBox, 1)
	user32.NewProc("EnableWindow").Call(stopButton, 0)
}
func begin() {
	if cancel != nil {
		return
	}
	dir := strings.Trim(strings.TrimSpace(textOf(pathEdit)), "\"")
	info, err := os.Stat(dir)
	if err != nil || !info.IsDir() {
		put(statusText, "Dossier introuvable : indiquez le dossier Gamelogs existant.")
		return
	}
	replay, _, _ := send.Call(replayBox, 0xF0, 0, 0)
	history = nil
	totals = map[string]int64{}
	count = 0
	lastError = ""
	put(eventsEdit, "")
	put(summaryEdit, "En attente d’événements…")
	ctx, c := context.WithCancel(context.Background())
	cancel = c
	ch := make(chan Update, 1024)
	updates = ch
	user32.NewProc("EnableWindow").Call(startButton, 0)
	user32.NewProc("EnableWindow").Call(pathEdit, 0)
	user32.NewProc("EnableWindow").Call(replayBox, 0)
	user32.NewProc("EnableWindow").Call(stopButton, 1)
	put(statusText, "Ouverture en lecture seule…")
	go watch(ctx, dir, replay == 1, ch)
}
func drain() {
	changed := false
	for i := 0; i < 2000 && updates != nil; i++ {
		select {
		case u, ok := <-updates:
			if !ok {
				updates = nil
				stop()
				if lastError != "" {
					put(statusText, lastError)
				} else {
					put(statusText, "Surveillance arrêtée — aucun fichier EVE modifié")
				}
				break
			}
			if u.Status != "" {
				if strings.HasPrefix(u.Status, "Surveillance active") {
					if lastError != "" {
						put(statusText, u.Status+" | "+lastError)
					} else {
						put(statusText, u.Status)
					}
				} else {
					lastError = u.Status
					put(statusText, lastError)
				}
			}
			if e := u.Event; e != nil {
				count++
				changed = true
				labels := map[string]string{"shield": "Shield reçu", "armor": "Armor reçue", "hull": "Structure reçue", "damage-in": "Dégâts reçus", "damage-out": "Dégâts infligés"}
				key := e.Listener + " | " + labels[e.Kind]
				if e.Kind == "shield" || e.Kind == "armor" || e.Kind == "hull" {
					key += " | " + e.Actor
				}
				totals[key] += e.Amount
				history = append(history, fmt.Sprintf("%s | %s | %s : %d | %s | %s", e.Time, e.Listener, labels[e.Kind], e.Amount, e.Actor, e.Module))
				if len(history) > 300 {
					history = history[len(history)-300:]
				}
			}
		default:
			i = 2000
		}
	}
	if changed {
		keys := make([]string, 0, len(totals))
		for k := range totals {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		rows := []string{fmt.Sprintf("%d événements analysés depuis le démarrage de cette surveillance", count)}
		for _, k := range keys {
			rows = append(rows, fmt.Sprintf("%s : %d", k, totals[k]))
		}
		put(summaryEdit, strings.Join(rows, "\r\n"))
		put(eventsEdit, strings.Join(history, "\r\n"))
		send.Call(eventsEdit, 0xB1, ^uintptr(0), ^uintptr(0))
		send.Call(eventsEdit, 0xB7, 0, 0)
	}
}
func windowProc(hwnd uintptr, msg uint32, wp, lp uintptr) uintptr {
	switch msg {
	case 1:
		child(hwnd, "STATIC", "Dossier des logs de combat EVE", 0, 0)
		pathEdit = child(hwnd, "EDIT", defaultFolder(), 0x00800000|0x80, 0)
		child(hwnd, "BUTTON", "Parcourir…", 0x10000, 103)
		child(hwnd, "STATIC", "Lecture seule • Aucun envoi réseau • Les logs restent disponibles pour EVE et les autres outils", 0, 0)
		replayBox = child(hwnd, "BUTTON", "Relire aussi les fichiers existants (sinon : nouveaux événements seulement)", 3|0x10000, 0)
		startButton = child(hwnd, "BUTTON", "Démarrer", 0x10000, 101)
		stopButton = child(hwnd, "BUTTON", "Arrêter", 0x10000, 102)
		statusText = child(hwnd, "STATIC", "Prêt — choisissez le dossier puis cliquez sur Démarrer", 0, 0)
		summaryEdit = child(hwnd, "EDIT", "Totaux de la session", 0x00800000|0x00200000|0x4|0x800|0x40, 0)
		child(hwnd, "STATIC", "Derniers événements — horaires tels qu’enregistrés dans les logs", 0, 0)
		eventsEdit = child(hwnd, "EDIT", "", 0x00800000|0x00200000|0x00100000|0x4|0x800|0x40|0x80, 0)
		send.Call(eventsEdit, 0xC5, 1024*1024, 0)
		send.Call(summaryEdit, 0xC5, 1024*1024, 0)
		stop()
		user32.NewProc("SetTimer").Call(hwnd, 1, 250, 0)
		return 0
	case 5:
		layout(int32(lp&0xffff), int32((lp>>16)&0xffff))
		return 0
	case 0x111:
		switch wp & 0xffff {
		case 101:
			begin()
		case 103:
			browse(hwnd)
		case 102:
			stop()
			put(statusText, "Arrêt demandé…")
		}
		return 0
	case 0x113:
		drain()
		return 0
	case 2:
		stop()
		user32.NewProc("PostQuitMessage").Call(0)
		return 0
	}
	r, _, _ := user32.NewProc("DefWindowProcW").Call(hwnd, uintptr(msg), wp, lp)
	return r
}
func main() {
	runtime.LockOSThread()
	ole := syscall.NewLazyDLL("ole32.dll")
	ole.NewProc("OleInitialize").Call(0)
	defer ole.NewProc("OleUninitialize").Call()
	instance, _, _ := kernel32.NewProc("GetModuleHandleW").Call(0)
	class := wide("EVEOSINTCombatReader")
	cursor, _, _ := user32.NewProc("LoadCursorW").Call(0, 32512)
	wc := windowClass{Proc: syscall.NewCallback(windowProc), Instance: instance, Cursor: cursor, Background: 6, Name: class}
	wc.Size = uint32(unsafe.Sizeof(wc))
	atom, _, _ := user32.NewProc("RegisterClassExW").Call(uintptr(unsafe.Pointer(&wc)))
	if atom == 0 {
		return
	}
	hwnd, _, _ := createWindow.Call(0, uintptr(unsafe.Pointer(class)), uintptr(unsafe.Pointer(wide("EVEOSINT — Combat Log Reader — Prototype 0.1"))), 0x00CF0000|0x10000000, 100, 100, 1100, 740, 0, 0, instance, 0)
	if hwnd == 0 {
		return
	}
	var m message
	for {
		r, _, _ := user32.NewProc("GetMessageW").Call(uintptr(unsafe.Pointer(&m)), 0, 0, 0)
		if int32(r) <= 0 {
			break
		}
		user32.NewProc("TranslateMessage").Call(uintptr(unsafe.Pointer(&m)))
		user32.NewProc("DispatchMessageW").Call(uintptr(unsafe.Pointer(&m)))
	}
}
