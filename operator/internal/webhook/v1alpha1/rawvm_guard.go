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
	"fmt"
	"net/http"
	"os"
	"strings"

	admissionv1 "k8s.io/api/admission/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/webhook"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"
)

// RawVMGuardPath is where the guard listens.
const RawVMGuardPath = "/guard-kubevirt-io-v1-virtualmachine"

// AllowedSubjectsEnv lists the identities permitted to create a KubeVirt
// VirtualMachine directly, comma separated.
//
// It is configuration rather than a constant because the answer is a property
// of the cluster, not of this code. On the stand the tenant machinery creates
// worker VMs as system:serviceaccount:o0-capi:capk-manager, and that namespace
// is an install choice — a hardcoded literal would work here and break the
// first cluster that installs Cluster API somewhere else, by refusing to
// provision tenant workers.
const AllowedSubjectsEnv = "OPERATOR_RAW_VM_ALLOWED_SUBJECTS"

// RawVMGuard refuses hand-written KubeVirt VirtualMachine objects in managed
// namespaces.
//
// Without it the whole contract is optional: every rule the ManagedVM path
// enforces — which network a namespace may use, which image it may clone, the
// labels the product's own views filter on — applies only to those who choose
// to go through it. Measured on this cluster, KubeVirt does not stop a
// cross-namespace clone either, so a raw VirtualMachine is also the way to read
// another team's disk. This is the fence around that.
type RawVMGuard struct {
	// AllowedSubjects returns the identities that may still write raw objects.
	// A function, not a captured slice: a value read at import time freezes
	// whatever the environment held then, and no later change reaches it.
	AllowedSubjects func() []string
}

// The guard fails open on purpose. Failing closed would couple every VM
// creation on the cluster — including the tenant machinery replacing an
// unhealthy worker — to this operator being up, and an operator outage would
// then become a tenant outage. The trade is deliberate: during an outage the
// contract can be bypassed, which is recoverable and visible, rather than the
// cluster losing its ability to heal, which is not.
//
// +kubebuilder:webhook:path=/guard-kubevirt-io-v1-virtualmachine,mutating=false,failurePolicy=ignore,sideEffects=None,groups=kubevirt.io,resources=virtualmachines,verbs=create,versions=v1,name=guard-virtualmachine.kb.io,admissionReviewVersions=v1

// SetupRawVMGuardWithManager registers the guard on the webhook server.
func SetupRawVMGuardWithManager(mgr ctrl.Manager) error {
	guard := &RawVMGuard{AllowedSubjects: AllowedSubjectsFromEnv}
	mgr.GetWebhookServer().Register(RawVMGuardPath, &webhook.Admission{Handler: guard})
	return nil
}

// AllowedSubjectsFromEnv reads the allowlist at call time.
func AllowedSubjectsFromEnv() []string {
	raw := os.Getenv(AllowedSubjectsEnv)
	var out []string
	for _, item := range strings.Split(raw, ",") {
		if s := strings.TrimSpace(item); s != "" {
			out = append(out, s)
		}
	}
	return out
}

// Handle admits a raw VirtualMachine only from an allowed identity.
func (g *RawVMGuard) Handle(_ context.Context, req admission.Request) admission.Response {
	if req.Operation != admissionv1.Create {
		return admission.Allowed("")
	}

	user := req.UserInfo.Username
	for _, allowed := range g.allowed() {
		if user == allowed {
			return admission.Allowed("")
		}
	}

	// Log every refusal. The allowlist is a claim about which machinery on this
	// cluster creates VMs, and a claim like that is only ever verified by
	// watching what gets turned away — a controller silently unable to
	// provision looks like a slow cluster, not like a policy decision.
	managedvmlog.Info("refused a raw VirtualMachine",
		"user", user, "namespace", req.Namespace, "name", req.Name,
		"allowed", g.allowed())

	return admission.Errored(http.StatusForbidden, fmt.Errorf(
		"creating a KubeVirt VirtualMachine directly is not allowed in this namespace: "+
			"create a ManagedVM (platform.kubevirt-ui.io/v1alpha1) instead, so that the "+
			"network, image and quota rules apply. Identity %q is not in %s",
		user, AllowedSubjectsEnv))
}

func (g *RawVMGuard) allowed() []string {
	if g.AllowedSubjects == nil {
		return nil
	}
	return g.AllowedSubjects()
}
