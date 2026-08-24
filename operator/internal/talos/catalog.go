/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

// Package talos says which Talos versions this deployment offers and which
// Kubernetes they take.
//
// Curated, not discovered. The image factory serves images by version; the
// compatibility matrix lives in Talos's release notes, so "read it from the
// factory" means scraping documentation. A live external feed is also the
// wrong dependency for a private cloud, and "new versions without a release"
// is an anti-feature here: a Talos version must appear only after its golden
// image has been imported and smoked.
//
// One function owns the question. Every reader goes through Compatible,
// because the failure when they diverge is the one this codebase keeps
// meeting — the wizard offers a pair the backend then refuses, or accepts.
package talos

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

// emptySchematic is the factory's no-extensions image. A KubeVirt worker needs
// none.
//
// `openstack` and not `nocloud`: CAPK attaches a cloudInitConfigDrive, which is
// an OpenStack config-2 disk. The nocloud variant looks for a `cidata` disk,
// does not find one, and sits in maintenance mode waiting for
// `talosctl apply-config` — a worker that boots fine and never joins.
const emptySchematic = "376567988ad370138ad8b2698212367b8edcb69b5fd68c80be1f2ec7d603b4ba"

// Release is one offered Talos version and the Kubernetes window it supports.
type Release struct {
	Talos    string `json:"talos"`
	ImageURL string `json:"image_url,omitempty"`
	K8sMin   string `json:"k8s_min"`
	K8sMax   string `json:"k8s_max"`
	SHA      string `json:"sha,omitempty"`
	Default  bool   `json:"default,omitempty"`
}

// FactoryImageURL is where a version's image comes from.
func FactoryImageURL(version string) string {
	return fmt.Sprintf(
		"https://factory.talos.dev/image/%s/v%s/openstack-amd64.raw.xz",
		emptySchematic, strings.TrimPrefix(strings.TrimSpace(version), "v"))
}

// builtIn is what the lab runs today. Talos 1.13 supports Kubernetes 1.31–1.36.
var builtIn = []Release{{
	Talos:    "1.13.8",
	ImageURL: FactoryImageURL("1.13.8"),
	K8sMin:   "1.31",
	K8sMax:   "1.36",
	Default:  true,
}}

var minorPattern = regexp.MustCompile(`^v?(\d+)\.(\d+)`)

// minorOf is (major, minor), compared as numbers and never as text: "1.9"
// sorts above "1.31" as a string, which would silently accept a version
// outside the window.
func minorOf(version string) (int, int, bool) {
	match := minorPattern.FindStringSubmatch(strings.TrimSpace(version))
	if match == nil {
		return 0, 0, false
	}
	major, _ := strconv.Atoi(match[1])
	minor, _ := strconv.Atoi(match[2])
	return major, minor, true
}

// Compatible is the single fact. Every reader asks this and nothing else.
//
// Compared at minor granularity: the window is stated as 1.31–1.36 and a tenant
// asks for 1.32.1, so patch releases must not narrow it.
func Compatible(release Release, k8sVersion string) bool {
	wantMajor, wantMinor, ok := minorOf(k8sVersion)
	if !ok {
		return false
	}
	loMajor, loMinor, okLo := minorOf(release.K8sMin)
	hiMajor, hiMinor, okHi := minorOf(release.K8sMax)
	if !okLo || !okHi {
		return false
	}
	lo := loMajor*1000 + loMinor
	hi := hiMajor*1000 + hiMinor
	want := wantMajor*1000 + wantMinor
	return lo <= want && want <= hi
}

// Catalog is the offered releases.
//
// A malformed override falls back to the built-in list rather than leaving the
// deployment with no Talos at all, and returns the reason — the operator who
// wrote it will otherwise see their new version simply not appear.
func Catalog(override string) ([]Release, error) {
	override = strings.TrimSpace(override)
	if override == "" {
		return append([]Release(nil), builtIn...), nil
	}
	parsed, err := parse(override)
	if err != nil {
		return append([]Release(nil), builtIn...), fmt.Errorf(
			"the configured catalogue is not usable (%w); falling back to the "+
				"built-in list, so the versions configured there are NOT offered", err)
	}
	return parsed, nil
}

func parse(raw string) ([]Release, error) {
	var entries []Release
	if err := json.Unmarshal([]byte(raw), &entries); err != nil {
		return nil, err
	}
	if len(entries) == 0 {
		return nil, fmt.Errorf("catalogue is empty")
	}
	for i := range entries {
		if entries[i].Talos == "" || entries[i].K8sMin == "" || entries[i].K8sMax == "" {
			return nil, fmt.Errorf("entry %d is missing talos, k8s_min or k8s_max", i)
		}
		if entries[i].ImageURL == "" {
			entries[i].ImageURL = FactoryImageURL(entries[i].Talos)
		}
	}
	return entries, nil
}

// DefaultRelease is the one to preselect: the first marked default, otherwise
// the first. A catalogue with no default is a configuration slip, not a reason
// to refuse to create anything.
func DefaultRelease(entries []Release) (Release, bool) {
	for _, entry := range entries {
		if entry.Default {
			return entry, true
		}
	}
	if len(entries) > 0 {
		return entries[0], true
	}
	return Release{}, false
}

// Find looks a version up, tolerating the leading v the UI sometimes sends.
func Find(entries []Release, version string) (Release, bool) {
	wanted := strings.TrimPrefix(strings.TrimSpace(version), "v")
	for _, entry := range entries {
		if entry.Talos == wanted {
			return entry, true
		}
	}
	return Release{}, false
}

// CompatiblePairs is every (talos, k8s-minor) this deployment will accept.
//
// For the anti-drift test: what is offered and what is accepted have to be the
// same set, and comparing them is testing the meaning rather than copying the
// implementation.
func CompatiblePairs(entries []Release) map[[2]string]bool {
	pairs := map[[2]string]bool{}
	for _, entry := range entries {
		loMajor, loMinor, okLo := minorOf(entry.K8sMin)
		_, hiMinor, okHi := minorOf(entry.K8sMax)
		if !okLo || !okHi {
			continue
		}
		for minor := loMinor; minor <= hiMinor; minor++ {
			pairs[[2]string{entry.Talos, fmt.Sprintf("%d.%d", loMajor, minor)}] = true
		}
	}
	return pairs
}

// Refusal is the sentence a caller gets when the pair does not fit, or "".
//
// Worded exactly as the endpoint already words it. A user who is shown two
// different explanations of one rule concludes there are two rules, and the
// wizard renders its list from the same catalogue this refuses from.
func Refusal(entries []Release, talosVersion, k8sVersion string) string {
	release, ok := Find(entries, talosVersion)
	if !ok {
		offered := make([]string, 0, len(entries))
		for _, entry := range entries {
			offered = append(offered, entry.Talos)
		}
		return fmt.Sprintf(
			"Talos %s is not in this deployment's catalogue. Offered: %s.",
			talosVersion, strings.Join(offered, ", "))
	}
	if !Compatible(release, k8sVersion) {
		pairs := make([]string, 0, len(entries))
		for _, entry := range entries {
			pairs = append(pairs, fmt.Sprintf(
				"Talos %s -> Kubernetes %s-%s", entry.Talos, entry.K8sMin, entry.K8sMax))
		}
		return fmt.Sprintf(
			"Talos %s does not support Kubernetes %s (it takes %s-%s). "+
				"Compatible pairs: %s.",
			release.Talos, k8sVersion, release.K8sMin, release.K8sMax,
			strings.Join(pairs, ", "))
	}
	return ""
}
