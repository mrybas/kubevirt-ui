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

package controller

import (
	"context"
	"fmt"
	"os"

	rbacv1 "k8s.io/api/rbac/v1"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
	"github.com/mrybas/kubevirt-ui/operator/internal/talos"
)

const (
	goldenNamespaceEnv     = "TENANTS_GOLDEN_NAMESPACE"
	goldenStorageClassEnv  = "TENANTS_GOLDEN_STORAGE_CLASS"
	defaultGoldenNamespace = "kubevirt-ui-system"

	// goldenSize is the disk the image is imported into, not the disk a worker
	// ends up with: the clone is resized to whatever the tenant asked for.
	goldenSize = "20Gi"

	// goldenClonerRole grants `datavolumes/source`, which is what CDI's webhook
	// checks in the SOURCE namespace before it will allow a cross-namespace
	// clone.
	goldenClonerRole = "talos-golden-cloner"
)

// goldenNamespace holds the shared images. Deliberately not a tenant namespace:
// an image every tenant clones cannot live inside one of them, or deleting that
// tenant would take the others' source disk with it.
func goldenNamespace() string {
	if name := os.Getenv(goldenNamespaceEnv); name != "" {
		return name
	}
	return defaultGoldenNamespace
}

// reconcileGolden makes sure the Talos image this tenant clones from exists, and
// that the tenant is allowed to clone it.
//
// It writes a ManagedImage rather than a DataVolume, and that is the point of
// moving it here: importing disks is already a controller's job, and the tenant
// declaring "I need this image" instead of importing one itself is one writer
// where there were two. The product's own version waited up to twenty seconds
// inside an HTTP request for an import it had just started; a controller has
// somewhere to put "not yet".
//
// Nothing here is owned by the tenant. The image outlives every tenant that
// clones it — an ownerReference would make the first tenant deleted take the
// shared disk away from the rest.
func (r *ManagedTenantReconciler) reconcileGolden(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant, namespace, release string,
) (ready bool, message string, err error) {
	if obj.Spec.Workers.OS != "talos" || release == "" {
		// A cloud-init tenant clones nothing. Reporting a golden it will never
		// use would be a condition that can only ever be false.
		return true, "", nil
	}

	entries, _ := talos.Catalog(os.Getenv(tenantCatalogEnv))
	entry, found := talos.Find(entries, release)
	if !found || entry.ImageURL == "" {
		return false, fmt.Sprintf(
			"the catalogue has no image for Talos %s, so there is nothing to "+
				"import and nothing for the workers to clone", release), nil
	}

	goldenNS := goldenNamespace()
	name := talos.GoldenName(release)

	image := &platformv1alpha1.ManagedImage{}
	image.Name = name
	image.Namespace = goldenNS
	if _, err := kube.Ensure(ctx, r.Client, tenantControllerName, image, func() error {
		if image.Labels == nil {
			image.Labels = map[string]string{}
		}
		image.Labels["kubevirt-ui.io/managed"] = "true"
		image.Labels["kubevirt-ui.io/talos-golden"] = "true"
		image.Labels["kubevirt-ui.io/talos-version"] = release
		image.Spec.DisplayName = "Talos " + release
		image.Spec.Description = "Shared root image every Talos " + release +
			" worker is cloned from."
		image.Spec.Source = platformv1alpha1.ManagedImageSource{
			HTTP: &platformv1alpha1.HTTPSource{URL: entry.ImageURL},
		}
		image.Spec.Size = goldenSize
		image.Spec.StorageClass = os.Getenv(goldenStorageClassEnv)
		image.Spec.OSType = "talos"
		image.Spec.OSVersion = release
		return nil
	}); err != nil {
		return false, "", fmt.Errorf("declaring the shared Talos image %s/%s: %w",
			goldenNS, name, err)
	}

	if err := r.ensureGoldenCloner(ctx, goldenNS, namespace); err != nil {
		return false, "", err
	}

	live := &platformv1alpha1.ManagedImage{}
	if err := r.Get(ctx, types.NamespacedName{Namespace: goldenNS, Name: name}, live); err != nil {
		return false, "", fmt.Errorf("reading the shared Talos image %s/%s: %w",
			goldenNS, name, err)
	}
	if apimeta.IsStatusConditionTrue(live.Status.Conditions, platformv1alpha1.ConditionReady) {
		return true, fmt.Sprintf("%s/%s is imported and cloneable", goldenNS, name), nil
	}
	phase := live.Status.Phase
	if phase == "" {
		phase = "Pending"
	}
	// Not a refusal. CDI's clone waits for its source anyway, so a golden still
	// importing delays the first worker's disk rather than failing the tenant.
	return false, fmt.Sprintf("%s/%s is %s; the workers' disks wait for it",
		goldenNS, name, phase), nil
}

// ensureGoldenCloner grants the clone, in the namespace that holds the source.
//
// Two subjects sit on this path and they are different. The backend creating
// the tenant is not the one that clones: the worker's root disk is a
// dataVolumeTemplate on the VirtualMachine, so KubeVirt creates it, and CDI
// evaluates the tenant namespace's default ServiceAccount — which is what the
// cluster said in as many words:
//
//	not authorized to create DataVolume: User
//	system:serviceaccount:tenant-ga:default has insufficient permissions
//	in clone source namespace kubevirt-ui-system
func (r *ManagedTenantReconciler) ensureGoldenCloner(
	ctx context.Context, goldenNS, tenantNS string,
) error {
	role := &rbacv1.Role{}
	role.Name = goldenClonerRole
	role.Namespace = goldenNS
	if _, err := kube.Ensure(ctx, r.Client, tenantControllerName, role, func() error {
		if role.Labels == nil {
			role.Labels = map[string]string{}
		}
		role.Labels["kubevirt-ui.io/managed"] = "true"
		role.Rules = []rbacv1.PolicyRule{{
			APIGroups: []string{"cdi.kubevirt.io"},
			Resources: []string{"datavolumes/source"},
			Verbs:     []string{"create"},
		}}
		return nil
	}); err != nil {
		return fmt.Errorf("granting the clone in %s: %w", goldenNS, err)
	}

	binding := &rbacv1.RoleBinding{}
	binding.Name = goldenClonerRole + "-" + tenantNS
	binding.Namespace = goldenNS
	if _, err := kube.Ensure(ctx, r.Client, tenantControllerName, binding, func() error {
		if binding.Labels == nil {
			binding.Labels = map[string]string{}
		}
		binding.Labels["kubevirt-ui.io/managed"] = "true"
		binding.RoleRef = rbacv1.RoleRef{
			APIGroup: rbacv1.GroupName, Kind: "Role", Name: goldenClonerRole,
		}
		binding.Subjects = []rbacv1.Subject{{
			Kind: "ServiceAccount", Name: "default", Namespace: tenantNS,
		}}
		return nil
	}); err != nil {
		return fmt.Errorf("binding the clone for %s: %w", tenantNS, err)
	}
	return nil
}

// goldenBindingsOf lists the clone grants pointing at one tenant namespace.
// Used by the teardown, and by the test that checks a second tenant adds a
// binding rather than replacing the first one's.
func (r *ManagedTenantReconciler) goldenBindingsOf(
	ctx context.Context, tenantNS string,
) ([]rbacv1.RoleBinding, error) {
	bindings := &rbacv1.RoleBindingList{}
	if err := r.List(ctx, bindings, client.InNamespace(goldenNamespace())); err != nil {
		return nil, err
	}
	var mine []rbacv1.RoleBinding
	for _, binding := range bindings.Items {
		for _, subject := range binding.Subjects {
			if subject.Kind == "ServiceAccount" && subject.Namespace == tenantNS {
				mine = append(mine, binding)
				break
			}
		}
	}
	return mine, nil
}

func goldenCondition(ready bool, message string) metav1.Condition {
	status := metav1.ConditionFalse
	reason := "Importing"
	if ready {
		status = metav1.ConditionTrue
		reason = "Ready"
	}
	return metav1.Condition{
		Type:    platformv1alpha1.ConditionGoldenReady,
		Status:  status,
		Reason:  reason,
		Message: message,
	}
}
