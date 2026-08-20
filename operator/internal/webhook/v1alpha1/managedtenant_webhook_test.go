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

package v1alpha1

import (
	"context"
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
)

func tenantFor(k8s, workerOS, talosVersion string) *platformv1alpha1.ManagedTenant {
	return &platformv1alpha1.ManagedTenant{
		ObjectMeta: metav1.ObjectMeta{Name: "t1"},
		Spec: platformv1alpha1.ManagedTenantSpec{
			DisplayName:       "T1",
			Folder:            "poc",
			Environment:       "dev",
			KubernetesVersion: k8s,
			Workers: platformv1alpha1.TenantWorkers{
				Count: 2, VCPU: 2, Memory: "2Gi", Disk: "20Gi",
				OS: workerOS, TalosVersion: talosVersion,
			},
			ControlPlaneReplicas: 2,
		},
	}
}

// TestAnIncompatiblePairIsRefusedAtAdmission.
//
// A refusal and not a condition, and the line is whether waiting could ever
// help. A missing network may appear a second later; no amount of waiting makes
// Talos 1.13 support Kubernetes 1.30. Refusing at admission also puts the
// sentence where the person is, rather than in a status field they have to go
// and find.
func TestAnIncompatiblePairIsRefusedAtAdmission(t *testing.T) {
	validator := &ManagedTenantCustomValidator{}

	_, err := validator.ValidateCreate(context.Background(),
		tenantFor("v1.30.1", "talos", "1.13.8"))
	if err == nil {
		t.Fatal("Talos 1.13.8 was accepted for Kubernetes 1.30.1")
	}
	// Word for word what the endpoint says. A user shown two explanations of
	// one rule concludes there are two rules.
	for _, phrase := range []string{
		"does not support Kubernetes v1.30.1",
		"it takes 1.31-1.36",
		"Compatible pairs:",
	} {
		if !strings.Contains(err.Error(), phrase) {
			t.Errorf("the refusal does not say %q: %v", phrase, err)
		}
	}
}

// TestACompatiblePairGoesThrough, including the default release when none is
// named.
func TestACompatiblePairGoesThrough(t *testing.T) {
	validator := &ManagedTenantCustomValidator{}

	if _, err := validator.ValidateCreate(context.Background(),
		tenantFor("v1.33.1", "talos", "1.13.8")); err != nil {
		t.Errorf("a compatible pair was refused: %v", err)
	}
	if _, err := validator.ValidateCreate(context.Background(),
		tenantFor("v1.33.1", "talos", "")); err != nil {
		t.Errorf("the catalogue default was refused: %v", err)
	}
}

// TestAnUnknownReleaseIsRefusedByName.
func TestAnUnknownReleaseIsRefusedByName(t *testing.T) {
	validator := &ManagedTenantCustomValidator{}
	_, err := validator.ValidateCreate(context.Background(),
		tenantFor("v1.33.1", "talos", "9.9.9"))
	if err == nil {
		t.Fatal("a release nobody offers was accepted")
	}
	if !strings.Contains(err.Error(), "not in this deployment's catalogue") {
		t.Errorf("refusal = %v", err)
	}
}

// TestACloudInitPoolIsNotCheckedAgainstTheTalosWindow — it has nothing to do
// with Talos, and refusing it would invent a rule.
func TestACloudInitPoolIsNotCheckedAgainstTheTalosWindow(t *testing.T) {
	validator := &ManagedTenantCustomValidator{}
	if _, err := validator.ValidateCreate(context.Background(),
		tenantFor("v1.30.1", "cloud-init", "")); err != nil {
		t.Errorf("a cloud-init tenant was refused for a Talos reason: %v", err)
	}
}

// TestAVersionThatWouldBeIgnoredIsRefused. Saying nothing would let somebody
// believe they had chosen a version.
func TestAVersionThatWouldBeIgnoredIsRefused(t *testing.T) {
	validator := &ManagedTenantCustomValidator{}
	_, err := validator.ValidateCreate(context.Background(),
		tenantFor("v1.30.1", "cloud-init", "1.13.8"))
	if err == nil {
		t.Fatal("a Talos version on a cloud-init pool was accepted in silence")
	}
	if !strings.Contains(err.Error(), "would be ignored") {
		t.Errorf("refusal = %v", err)
	}
}

// TestAnUnsizeableTenantIsRefused: a quantity that cannot be parsed cannot be
// reserved, and the tenant would otherwise sit unbuilt with the reason in a
// field nobody is looking at.
func TestAnUnsizeableTenantIsRefused(t *testing.T) {
	validator := &ManagedTenantCustomValidator{}
	obj := tenantFor("v1.33.1", "cloud-init", "")
	obj.Spec.Workers.Memory = "two gigabytes"

	_, err := validator.ValidateCreate(context.Background(), obj)
	if err == nil {
		t.Fatal("a tenant with an unparseable memory size was accepted")
	}
	if !strings.Contains(err.Error(), "cannot size this tenant") {
		t.Errorf("refusal = %v", err)
	}
}

// TestUpdateIsCheckedToo. A pair edited into an incompatible one is no better
// than one created that way, and the path this replaces checked only create.
func TestUpdateIsCheckedToo(t *testing.T) {
	validator := &ManagedTenantCustomValidator{}
	good := tenantFor("v1.33.1", "talos", "1.13.8")
	bad := tenantFor("v1.30.1", "talos", "1.13.8")

	if _, err := validator.ValidateUpdate(context.Background(), good, bad); err == nil {
		t.Fatal("an incompatible pair was edited in")
	}
}
