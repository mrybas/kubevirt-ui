// Command chartsync writes the operator's generated manifests into the chart.
//
//	go run ./cmd/chartsync            # write
//	go run ./cmd/chartsync --check    # fail if they are out of date
package main

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/mrybas/kubevirt-ui/operator/internal/chartsync"
)

func main() {
	check := flag.Bool("check", false, "report drift instead of fixing it")
	root := flag.String("root", "..", "path to the repository root")
	flag.Parse()

	absolute, err := filepath.Abs(*root)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	drifted, err := chartsync.Drifted(absolute, *check)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	if len(drifted) == 0 {
		fmt.Println("in sync")
		return
	}
	if *check {
		fmt.Fprintf(os.Stderr, "out of sync: %s\n", strings.Join(drifted, ", "))
		os.Exit(1)
	}
	fmt.Printf("wrote %s\n", strings.Join(drifted, ", "))
}
