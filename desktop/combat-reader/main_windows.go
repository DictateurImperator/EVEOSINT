package main

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
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
var pathEdit, replayBox, startButton, stopButton, statusText, summaryEdit, reportEdit uintptr
var pilotEdit, shipEdit, lossEdit uintptr
var updates <-chan Update
var cancel context.CancelFunc
var store repairStore
var knownPilots = map[string]bool{}
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
	col := max(int32(80), (w-64)/3)
	positions := [][4]int32{
		{16, 12, w - 32, 24}, {16, 40, w - 170, 28}, {w - 144, 40, 128, 28},
		{16, 78, w - 32, 24}, {16, 108, w - 32, 26}, {16, 142, 130, 30}, {158, 142, 130, 30},
		{16, 182, w - 32, 28}, {16, 218, w - 32, 24}, {16, 246, w - 32, 120},
		{16, 380, w - 32, 24}, {16, 414, col, 22}, {16, 438, col, 200},
		{32 + col, 414, col, 22}, {32 + col, 438, col, 28},
		{48 + 2*col, 414, col, 22}, {48 + 2*col, 438, col, 28},
		{16, 480, 200, 30}, {230, 480, 210, 30}, {16, 522, w - 32, max(int32(40), h-538)},
	}
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
	store = repairStore{}
	knownPilots = map[string]bool{}
	send.Call(pilotEdit, 0x14B, 0, 0) // CB_RESETCONTENT
	put(pilotEdit, "")
	put(shipEdit, "")
	put(lossEdit, "")
	lastError = ""
	put(reportEdit, "")
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
				if err := store.add(*e); err != nil {
					lastError = err.Error()
					continue
				}
				changed = true
				if !knownPilots[e.Listener] {
					knownPilots[e.Listener] = true
					send.Call(pilotEdit, 0x143, 0, uintptr(unsafe.Pointer(wide(e.Listener))))
					if textOf(pilotEdit) == "" {
						put(pilotEdit, e.Listener)
					}
				}
			}
		default:
			i = 2000
		}
	}
	if changed {
		put(summaryEdit, store.liveSummary())
	}
}
func prepareReport() (string, error) {
	loss := Loss{Pilot: strings.TrimSpace(textOf(pilotEdit)), Ship: strings.TrimSpace(textOf(shipEdit)), Time: strings.TrimSpace(textOf(lossEdit))}
	p, err := store.prepare(loss)
	if err != nil {
		put(reportEdit, "Rapport non préparé : "+err.Error())
		return "", err
	}
	raw, err := p.JSON()
	if err != nil {
		return "", err
	}
	put(reportEdit, p.Summary()+fmt.Sprintf("\r\n\r\nFormat EVEOSINT : JSON compact, %d octets. Aucun envoi effectué.", len(raw)))
	return raw, nil
}

func windowProc(hwnd uintptr, msg uint32, wp, lp uintptr) uintptr {
	switch msg {
	case 1:
		child(hwnd, "STATIC", "Dossier des logs de combat EVE", 0, 0)
		pathEdit = child(hwnd, "EDIT", defaultFolder(), 0x00800000|0x80, 0)
		child(hwnd, "BUTTON", "Parcourir…", 0x10000, 103)
		child(hwnd, "STATIC", "Lecture seule • Aucun envoi réseau • Aucune modification des logs ou de leurs permissions", 0, 0)
		replayBox = child(hwnd, "BUTTON", "Relire les fichiers existants (sinon : nouveaux événements seulement)", 3|0x10000, 0)
		startButton = child(hwnd, "BUTTON", "Démarrer", 0x10000, 101)
		stopButton = child(hwnd, "BUTTON", "Arrêter", 0x10000, 102)
		statusText = child(hwnd, "STATIC", "Prêt — choisissez le dossier puis cliquez sur Démarrer", 0, 0)
		child(hwnd, "STATIC", "Soins reçus — résumé des 30 dernières minutes de logs par personnage", 0, 0)
		summaryEdit = child(hwnd, "EDIT", "En attente de soins…", 0x00800000|0x00200000|0x4|0x800|0x40, 0)
		child(hwnd, "STATIC", "Préparer une perte — renseignez les informations du killmail (détection automatique à venir)", 0, 0)
		child(hwnd, "STATIC", "Pilote soigné", 0, 0)
		pilotEdit = child(hwnd, "COMBOBOX", "", 0x00200000|0x10000|2, 201)
		child(hwnd, "STATIC", "Vaisseau perdu", 0, 0)
		shipEdit = child(hwnd, "EDIT", "", 0x00800000|0x80|0x10000, 202)
		child(hwnd, "STATIC", "Heure de perte UTC : AAAA.MM.JJ HH:MM:SS", 0, 0)
		lossEdit = child(hwnd, "EDIT", "", 0x00800000|0x80|0x10000, 203)
		child(hwnd, "BUTTON", "Préparer le résumé", 0x10000, 104)
		child(hwnd, "BUTTON", "Copier pour EVEOSINT", 0x10000, 105)
		reportEdit = child(hwnd, "EDIT", "Le résumé regroupe les soins par pilote logi et par vaisseau. La copie contient uniquement le JSON compact.", 0x00800000|0x00200000|0x4|0x800|0x40, 0)
		send.Call(summaryEdit, 0xC5, 1024*1024, 0)
		send.Call(reportEdit, 0xC5, 1024*1024, 0)
		stop()
		user32.NewProc("SetTimer").Call(hwnd, 1, 250, 0)
		return 0
	case 5:
		layout(int32(lp&0xffff), int32((lp>>16)&0xffff))
		return 0
	case 0x111:
		switch wp & 0xffff {
		case 104:
			prepareReport()
		case 105:
			if raw, err := prepareReport(); err == nil {
				if err = copyText(hwnd, raw); err != nil {
					put(reportEdit, err.Error())
				} else {
					put(reportEdit, textOf(reportEdit)+"\r\nCopié dans le presse-papiers.")
				}
			}
		case 201, 202, 203:
			if reportEdit != 0 {
				put(reportEdit, "Informations de perte modifiées : préparez à nouveau le résumé.")
			}
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
	hwnd, _, _ := createWindow.Call(0, uintptr(unsafe.Pointer(class)), uintptr(unsafe.Pointer(wide("EVEOSINT — Combat Log Reader — Prototype 0.2"))), 0x00CF0000|0x10000000, 100, 100, 1100, 850, 0, 0, instance, 0)
	if hwnd == 0 {
		return
	}
	var m message
	for {
		r, _, _ := user32.NewProc("GetMessageW").Call(uintptr(unsafe.Pointer(&m)), 0, 0, 0)
		if int32(r) <= 0 {
			break
		}
		handled, _, _ := user32.NewProc("IsDialogMessageW").Call(hwnd, uintptr(unsafe.Pointer(&m)))
		if handled != 0 {
			continue
		}
		user32.NewProc("TranslateMessage").Call(uintptr(unsafe.Pointer(&m)))
		user32.NewProc("DispatchMessageW").Call(uintptr(unsafe.Pointer(&m)))
	}
}
