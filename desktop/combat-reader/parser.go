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
	FromSystem, ToSystem                              string
}

var tags = regexp.MustCompile(`<[^>]*>`)
var envelope = regexp.MustCompile(`^\[ ([0-9.]+ [0-9:]+) \] \(combat\) (.*)$`)
var damage = regexp.MustCompile(`^(\d+) (from|to) (.+?) - (.+)$`)
var rep = regexp.MustCompile(`^(\d+) (remote shield boosted|remote armor repaired|remote hull repaired) by (.+?) - - (.+)$`)
var brackets = regexp.MustCompile(`\[([^\]]+)\]`)

// Only structured combat events leave the parser. Chat, URLs and travel notices do not.
func parseLine(line, listener, source string) (Event, bool) {
	clean := html.UnescapeString(tags.ReplaceAllString(line, ""))
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

// These movement messages have been observed in the provided client logs. They
// remain local inputs to the receipt calculation, not data for transmission.
var lifecycleEnvelope = regexp.MustCompile(`^\[ ([0-9.]+ [0-9:]+) \] \(None\) (.*)$`)
var jumpMessage = regexp.MustCompile(`^Jumping from (.+) to (.+)$`)
var undockMessage = regexp.MustCompile(`^Undocking from (.+) to (.+) solar system\.$`)

func parseMovement(line, listener, source string) (Event, bool) {
	clean := html.UnescapeString(tags.ReplaceAllString(line, ""))
	m := lifecycleEnvelope.FindStringSubmatch(strings.TrimSpace(clean))
	if m == nil {
		return Event{}, false
	}
	e := Event{Time: m[1], Listener: listener, Source: source}
	if j := jumpMessage.FindStringSubmatch(m[2]); j != nil {
		e.Kind = "jump"
		e.FromSystem = j[1]
		e.ToSystem = j[2]
		return e, true
	}
	if u := undockMessage.FindStringSubmatch(m[2]); u != nil {
		e.Kind = "undock"
		e.ToSystem = u[2]
		return e, true
	}
	return Event{}, false
}
