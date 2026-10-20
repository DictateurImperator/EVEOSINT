package main

import (
	"html"
	"regexp"
	"strconv"
	"strings"
)

type Event struct {
	Time, Listener, Kind, Actor, Ship, Module, Source string
	Amount                                            int64
}

var tags = regexp.MustCompile(`<[^>]*>`)
var deathNotice = regexp.MustCompile(`^\[ ([0-9.]+ [0-9:]+) \] \(notify\) Ship is out of control$`)
var envelope = regexp.MustCompile(`^\[ ([0-9.]+ [0-9:]+) \] \(combat\) (.*)$`)
var damage = regexp.MustCompile(`^(\d+) (from|to) (.+?) - (.+)$`)
var rep = regexp.MustCompile(`^(\d+) (remote shield boosted|remote armor repaired|remote hull repaired) by (.+?) - - (.+)$`)
var brackets = regexp.MustCompile(`\[([^\]]+)\]`)

// Only structured combat events leave the parser. Chat, URLs and travel notices do not.
func parseLine(line, listener, source string) (Event, bool) {
	clean := html.UnescapeString(tags.ReplaceAllString(line, ""))
	if m := deathNotice.FindStringSubmatch(strings.TrimSpace(clean)); m != nil {
		return Event{Time: m[1], Listener: listener, Source: source, Kind: "loss-candidate"}, true
	}
	m := envelope.FindStringSubmatch(strings.TrimSpace(clean))
	if m == nil {
		return Event{}, false
	}
	e := Event{Time: m[1], Listener: listener, Source: source}
	if r := rep.FindStringSubmatch(m[2]); r != nil {
		e.Amount, _ = strconv.ParseInt(r[1], 10, 64)
		switch r[2] {
		case "remote shield boosted":
			e.Kind = "shield"
		case "remote armor repaired":
			e.Kind = "armor"
		default:
			e.Kind = "hull"
		}
		names := brackets.FindAllStringSubmatch(r[3], -1)
		if len(names) == 0 {
			e.Actor = strings.TrimSpace(r[3])
		} else {
			e.Actor = names[len(names)-1][1]
		}
		e.Ship = strings.TrimSpace(strings.SplitN(r[3], "[", 2)[0])
		e.Module = r[4]
		return e, true
	}
	if d := damage.FindStringSubmatch(m[2]); d != nil {
		e.Amount, _ = strconv.ParseInt(d[1], 10, 64)
		e.Actor = d[3]
		e.Module = d[4]
		if d[2] == "from" {
			e.Kind = "damage-in"
		} else {
			e.Kind = "damage-out"
		}
		return e, true
	}
	return Event{}, false
}
