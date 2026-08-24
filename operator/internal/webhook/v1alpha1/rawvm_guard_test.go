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

	admissionv1 "k8s.io/api/admission/v1"
	authenticationv1 "k8s.io/api/authentication/v1"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"
)

func request(user string, op admissionv1.Operation) admission.Request {
	return admission.Request{AdmissionRequest: admissionv1.AdmissionRequest{
		Operation: op,
		UserInfo:  authenticationv1.UserInfo{Username: user},
	}}
}

func guardAllowing(subjects ...string) *RawVMGuard {
	return &RawVMGuard{AllowedSubjects: func() []string { return subjects }}
}

func TestAPersonCannotHandWriteAVirtualMachine(t *testing.T) {
	got := guardAllowing("system:serviceaccount:kubevirt-ui-operator-dev:controller").
		Handle(context.Background(), request("kv-devadmin", admissionv1.Create))
	if got.Allowed {
		t.Fatal("a raw VirtualMachine was accepted from an ordinary user")
	}
	if !strings.Contains(got.Result.Message, "ManagedVM") {
		t.Fatalf("the refusal does not say what to do instead: %q", got.Result.Message)
	}
}

// The tenant machinery creates worker VMs itself. A guard that forgets it does
// not tighten policy, it stops tenants from provisioning — which is why the
// allowlist is configuration and why this case has a test of its own.
func TestTheTenantMachineryIsStillAllowed(t *testing.T) {
	got := guardAllowing("system:serviceaccount:o0-capi:capk-manager").
		Handle(context.Background(), request("system:serviceaccount:o0-capi:capk-manager", admissionv1.Create))
	if !got.Allowed {
		t.Fatalf("the tenant controller was refused: %q", got.Result.Message)
	}
}

func TestUpdatesAreNotTheGuardsBusiness(t *testing.T) {
	// The guard is about who may bring a VM into existence. KubeVirt itself,
	// and the operator, update these objects constantly.
	got := guardAllowing().Handle(context.Background(), request("anyone", admissionv1.Update))
	if !got.Allowed {
		t.Fatal("an update was refused by a guard that only governs creation")
	}
}

func TestAnEmptyAllowlistStillRefusesPeople(t *testing.T) {
	// Misconfiguration must fail closed for humans rather than open: an empty
	// list means nobody was declared, not that everybody is welcome.
	got := guardAllowing().Handle(context.Background(), request("kv-admin", admissionv1.Create))
	if got.Allowed {
		t.Fatal("an empty allowlist admitted a raw VirtualMachine")
	}
}
