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

// Package naming holds the label and annotation vocabulary the product already
// speaks, plus the slug rule the UI has always used.
//
// The operator writes the same keys the FastAPI backend writes, because the
// backend's listers, the Velero selectors and the UI filters all read them. A
// second vocabulary would mean objects the product creates but cannot see.
package naming

import (
	"regexp"
	"strings"
)

// Labels and annotations the existing product reads. Keys are quoted from the
// backend, not invented here.
const (
	ManagedLabel     = "kubevirt-ui.io/managed"
	SlugLabel        = "kubevirt-ui.io/slug"
	ScopeLabel       = "kubevirt-ui.io/scope"
	ProjectLabel     = "kubevirt-ui.io/project"
	EnvironmentLabel = "kubevirt-ui.io/environment"
	FolderLabel      = "kubevirt-ui.io/folder"
	DiskTypeLabel    = "kubevirt-ui.io/disk-type"
	PersistentLabel  = "kubevirt-ui.io/persistent"
	OSTypeLabel      = "kubevirt-ui.io/os-type"
	OSVersionLabel   = "kubevirt-ui.io/os-version"

	DisplayNameAnnotation = "kubevirt-ui.io/display-name"
	DescriptionAnnotation = "kubevirt-ui.io/description"
)

// The operator's own vocabulary, used to find children without parsing names.
const (
	// OwnerUIDLabel ties a generated object to the UID of the custom resource
	// that asked for it. UID rather than name: a renamed display name must not
	// orphan a disk, and a recreated resource must not adopt its predecessor's.
	OwnerUIDLabel = "platform.kubevirt-ui.io/owner-uid"
	// OwnerNameLabel is for humans reading kubectl output. Never for lookups.
	OwnerNameLabel = "platform.kubevirt-ui.io/owner-name"
	// OwnerKindLabel separates children of different custom resources that
	// happen to live in one namespace.
	OwnerKindLabel = "platform.kubevirt-ui.io/owner-kind"

	// AdoptAnnotation asks the controller to take over an object that already
	// exists instead of creating a new one. Adoption is always a deliberate,
	// per-object act — a reconciler that adopts on its own would argue with the
	// operator who left the object there.
	AdoptAnnotation = "platform.kubevirt-ui.io/adopt"
)

// Slug limits, copied from the backend so both produce the same label value.
const (
	dns1123Max = 63
	// The API server appends five characters to a generateName seed, plus the
	// separating dash.
	generateNameSuffixLen = 5
	slugMax               = dns1123Max - generateNameSuffixLen - 1
)

var nonAlnum = regexp.MustCompile(`[^a-z0-9]+`)

// Slug converts a human name into the same k8s-safe token the UI produces:
// lowercase, runs of non-alphanumerics collapsed to one dash, trimmed, capped,
// and never empty.
func Slug(displayName string) string {
	s := strings.ToLower(displayName)
	s = nonAlnum.ReplaceAllString(s, "-")
	s = strings.Trim(s, "-")
	if len(s) > slugMax {
		s = s[:slugMax]
	}
	s = strings.TrimRight(s, "-")
	if s == "" {
		return "unnamed"
	}
	return s
}
