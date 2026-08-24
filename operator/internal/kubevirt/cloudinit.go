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

package kubevirt

import (
	"fmt"
	"strings"
)

// MergeCloudInit composes the guest's user-data from the template's base, the
// VM's own document, the SSH keys and an optional first-boot password.
//
// The merge is textual, exactly as it has always been: cloud-config is YAML,
// but a structural merge would reorder and requote documents people wrote by
// hand and compare unequal against every template already stored.
//
// One deliberate difference from the handler this replaces: there, supplying
// your own user-data silently discarded the password, because the two lived on
// different branches. Here the password is always applied — a first-boot
// password that vanishes because of an unrelated field is not a behaviour worth
// preserving.
func MergeCloudInit(templateUserData, vmUserData string, sshKeys []string, password string) string {
	base := vmUserData
	if base == "" {
		base = templateUserData
	}
	if base == "" && (len(sshKeys) > 0 || password != "") {
		base = "#cloud-config\n"
	}
	if base == "" {
		return ""
	}

	if len(sshKeys) > 0 {
		if strings.Contains(base, "ssh_authorized_keys:") {
			// The document already opens the section; append into it.
			if !strings.HasSuffix(base, "\n") {
				base += "\n"
			}
			for _, key := range sshKeys {
				base += fmt.Sprintf("  - %s\n", key)
			}
		} else {
			base += "\nssh_authorized_keys:\n"
			for _, key := range sshKeys {
				base += fmt.Sprintf("  - %s\n", key)
			}
		}
	}

	if password != "" {
		base += fmt.Sprintf("\nchpasswd:\n  expire: false\npassword: %s\n", password)
	}

	return base
}
