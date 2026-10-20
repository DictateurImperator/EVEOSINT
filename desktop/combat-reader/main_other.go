//go:build !windows

package main

import (
	"fmt"
	"os"
)

func main() {
	fmt.Fprintln(os.Stderr, "Application graphique Windows. Les tests du lecteur peuvent être exécutés sur cet OS.")
}
