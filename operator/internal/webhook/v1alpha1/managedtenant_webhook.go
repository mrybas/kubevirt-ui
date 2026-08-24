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
	"os"

	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/talos"
	"github.com/mrybas/kubevirt-ui/operator/internal/tenant"
)

var managedtenantlog = logf.Log.WithName("managedtenant-resource")

// catalogEnv is where a deployment states which Talos versions it offers. The
// same variable the endpoint reads, because they must answer with one list.
const catalogEnv = "TENANTS_TALOS_CATALOG"

// SetupManagedTenantWebhookWithManager registers the webhook for ManagedTenant.
func SetupManagedTenantWebhookWithManager(mgr ctrl.Manager) error {
	return ctrl.NewWebhookManagedBy(mgr, &platformv1alpha1.ManagedTenant{}).
		WithValidator(&ManagedTenantCustomValidator{Client: mgr.GetClient()}).
		Complete()
}

// +kubebuilder:webhook:path=/validate-platform-kubevirt-ui-io-v1alpha1-managedtenant,mutating=false,failurePolicy=fail,sideEffects=None,groups=platform.kubevirt-ui.io,resources=managedtenants,verbs=create;update,versions=v1alpha1,name=vmanagedtenant-v1alpha1.kb.io,admissionReviewVersions=v1

// ManagedTenantCustomValidator refuses only what will never become true.
//
// The line matters, and it is the same one the rest of this operator draws: a
// missing network is a condition, because it may appear a second later and the
// tenant should wait; an incompatible pair of versions is a refusal, because no
// amount of waiting makes Talos 1.13 support Kubernetes 1.30. Refusing the
// second at admission puts the sentence where the person is, instead of in a
// status field they have to go and find.
type ManagedTenantCustomValidator struct {
	client.Client
}

// ValidateCreate refuses a tenant that cannot be built as written.
func (v *ManagedTenantCustomValidator) ValidateCreate(
	_ context.Context, obj *platformv1alpha1.ManagedTenant,
) (admission.Warnings, error) {
	managedtenantlog.V(1).Info("validating create", "name", obj.GetName())
	return validateTenant(obj)
}

// ValidateUpdate applies the same rules: a version pair edited into an
// incompatible one is no better than one created that way.
func (v *ManagedTenantCustomValidator) ValidateUpdate(
	_ context.Context, _, newObj *platformv1alpha1.ManagedTenant,
) (admission.Warnings, error) {
	managedtenantlog.V(1).Info("validating update", "name", newObj.GetName())
	return validateTenant(newObj)
}

// ValidateDelete has nothing to say.
func (v *ManagedTenantCustomValidator) ValidateDelete(
	_ context.Context, _ *platformv1alpha1.ManagedTenant,
) (admission.Warnings, error) {
	return nil, nil
}

func validateTenant(obj *platformv1alpha1.ManagedTenant) (admission.Warnings, error) {
	var warnings admission.Warnings

	entries, err := talos.Catalog(os.Getenv(catalogEnv))
	if err != nil {
		// The list fell back to the built-in one. Surfaced as a warning rather
		// than a refusal: the tenant being created is not the thing that is
		// misconfigured, and refusing it would hide the real problem behind an
		// unrelated failure.
		warnings = append(warnings, err.Error())
	}

	if obj.Spec.Workers.OS == "talos" {
		version := obj.Spec.Workers.TalosVersion
		if version == "" {
			release, ok := talos.DefaultRelease(entries)
			if !ok {
				return warnings, fmt.Errorf(
					"this deployment offers no Talos release at all")
			}
			version = release.Talos
		}
		if refusal := talos.Refusal(
			entries, version, obj.Spec.KubernetesVersion,
		); refusal != "" {
			return warnings, fmt.Errorf("%s", refusal)
		}
	} else if obj.Spec.Workers.TalosVersion != "" {
		// Asking for a Talos release on a cloud-init pool is a request that
		// cannot be honoured, and saying nothing would let somebody believe
		// they had chosen a version.
		return warnings, fmt.Errorf(
			"workers.talosVersion is set to %q but workers.os is %q; the "+
				"version would be ignored. Set workers.os to talos, or drop the "+
				"version", obj.Spec.Workers.TalosVersion, obj.Spec.Workers.OS)
	}

	// Sizing is refused here too, because a quantity that cannot be parsed
	// cannot be reserved and the tenant would sit unbuilt with the reason in a
	// field.
	if _, err := tenant.Reserve(sizingOf(obj)); err != nil {
		return warnings, fmt.Errorf("cannot size this tenant: %w", err)
	}

	return warnings, nil
}

// sizingOf is the tenant's request, in the shape the reservation takes.
func sizingOf(obj *platformv1alpha1.ManagedTenant) tenant.Sizing {
	workers := obj.Spec.Workers
	return tenant.Sizing{
		Workers:      int(orDefaultInt(workers.Count, 2)),
		VCPU:         int(orDefaultInt(workers.VCPU, 2)),
		Memory:       orDefault(workers.Memory, "2Gi"),
		Disk:         orDefault(workers.Disk, "20Gi"),
		CPReplicas:   int(orDefaultInt(obj.Spec.ControlPlaneReplicas, 2)),
		TalosWorkers: workers.OS == "talos",
	}
}

func orDefault(value, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}

func orDefaultInt(value, fallback int32) int32 {
	if value == 0 {
		return fallback
	}
	return value
}
