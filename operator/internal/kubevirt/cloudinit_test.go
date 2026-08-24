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
	"strings"
	"testing"
)

func TestNothingToMergeProducesNoDocument(t *testing.T) {
	// An empty document is not the same as "#cloud-config" with nothing in it:
	// the cloud-init disk is still attached for its network data, and adding an
	// empty user-data would change what every guest sees on first boot.
	if got := MergeCloudInit("", "", nil, ""); got != "" {
		t.Fatalf("expected no user-data, got %q", got)
	}
}

func TestKeysAloneStartAValidDocument(t *testing.T) {
	got := MergeCloudInit("", "", []string{"ssh-ed25519 AAAA"}, "")
	if !strings.HasPrefix(got, "#cloud-config") {
		t.Fatalf("document does not start with the cloud-config header: %q", got)
	}
	if !strings.Contains(got, "ssh_authorized_keys:\n  - ssh-ed25519 AAAA\n") {
		t.Fatalf("key not in its own section: %q", got)
	}
}

func TestKeysAppendIntoAnExistingSection(t *testing.T) {
	// A template that already opens the section must not get a second one —
	// cloud-init takes the last occurrence and the template's keys vanish.
	base := "#cloud-config\nssh_authorized_keys:\n  - ssh-rsa TEMPLATE\n"
	got := MergeCloudInit(base, "", []string{"ssh-ed25519 USER"}, "")
	if strings.Count(got, "ssh_authorized_keys:") != 1 {
		t.Fatalf("section duplicated: %q", got)
	}
	if !strings.Contains(got, "  - ssh-rsa TEMPLATE\n  - ssh-ed25519 USER\n") {
		t.Fatalf("keys not merged in order: %q", got)
	}
}

func TestTheVMDocumentReplacesTheTemplateBase(t *testing.T) {
	got := MergeCloudInit("#cloud-config\nruncmd:\n  - template\n", "#cloud-config\nruncmd:\n  - mine\n", nil, "")
	if strings.Contains(got, "template") {
		t.Fatalf("the template base leaked into an explicit document: %q", got)
	}
}

func TestThePasswordSurvivesAnExplicitDocument(t *testing.T) {
	// The handler this replaces dropped the password whenever user-data was
	// supplied, because the two sat on different branches. A first-boot password
	// that disappears because an unrelated field was filled in is a defect, not
	// a behaviour to carry over.
	got := MergeCloudInit("", "#cloud-config\nruncmd:\n  - mine\n", nil, "s3cret")
	if !strings.Contains(got, "chpasswd:\n  expire: false\npassword: s3cret\n") {
		t.Fatalf("password missing from the merged document: %q", got)
	}
}
