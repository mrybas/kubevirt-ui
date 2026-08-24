package controller

import (
	"fmt"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
)

/*
The folder ceiling, asked by the reconciler.

The API refused a tenant that did not fit its folder before writing anything;
this controller wrote the same quota from the same description and never asked.
So `spec.workers.count` edited on the CR — kubectl, GitOps, anything that is
not the API — grew the charge against the folder with nothing in the way, and
`ConditionQuotaReserved`, documented as "the folder ceiling's answer", was
never given one.

The arithmetic itself is held to `test/parity/folder-ceiling.json` by both
implementations. What is measured here is the wiring: what a refusal withholds,
what it leaves alone, and that failing to *ask* is not a refusal.
*/

// folderTree plants a ConfigMap the folder names below are read from. Folders
// not mentioned in it are uncapped, which is what keeps every other test in
// this package — all of them in folder "poc" — out of the ceiling's way.
func folderTree(t *testing.T, folders map[string]string) {
	t.Helper()
	cm := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: systemNamespace, Name: foldersConfigMap,
		},
		Data: folders,
	}
	err := k8sClient.Create(testCtx, cm)
	if apierrors.IsAlreadyExists(err) {
		live := &corev1.ConfigMap{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: systemNamespace, Name: foldersConfigMap,
		}, live); err != nil {
			t.Fatalf("reading the folder tree: %v", err)
		}
		live.Data = folders
		if err := k8sClient.Update(testCtx, live); err != nil {
			t.Fatalf("planting the folder tree: %v", err)
		}
		cm = live
	} else if err != nil {
		t.Fatalf("planting the folder tree: %v", err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, cm) })
}

func tenantIn(name, folder string) *platformv1alpha1.ManagedTenant {
	obj := plainTenant(name)
	obj.Spec.Folder = folder
	return obj
}

func condition(t *testing.T, name, kind string) *metav1.Condition {
	t.Helper()
	obj := getTenant(t, name)
	return apimeta.FindStatusCondition(obj.Status.Conditions, kind)
}

// TestAGrowthThatDoesNotFitIsHeldBack is the reported incident, from the side
// the report could not see: 72 CPU of quota accumulated under a folder that
// capped 32, because nothing here was looking.
func TestAGrowthThatDoesNotFitIsHeldBack(t *testing.T) {
	folderTree(t, map[string]string{
		"tight": `{"parent_id": null, "quota": {"cpu": "1"}}`,
	})
	mustTenant(t, tenantIn("tcf", "tight"))

	eventually(t, "the ceiling's refusal", func() error {
		c := condition(t, "tcf", platformv1alpha1.ConditionQuotaReserved)
		if c == nil || c.Status != metav1.ConditionFalse {
			return fmt.Errorf("QuotaReserved = %v", c)
		}
		if c.Reason != "DoesNotFit" {
			return fmt.Errorf("reason = %q", c.Reason)
		}
		// The arithmetic belongs in the message: a refusal nobody can act on
		// is a refusal nobody can act on.
		if !strings.Contains(c.Message, "is free and") ||
			!strings.Contains(c.Message, "tight") {
			return fmt.Errorf("message = %q", c.Message)
		}
		return nil
	})

	// The quota is the write that carries the growth, so it is not made at all.
	if _, err := readQuota("tenant-tcf-quota", "tenant-tcf"); err == nil {
		t.Fatal("a quota was written for a tenant the folder refused")
	} else if !apierrors.IsNotFound(err) {
		t.Fatalf("reading the quota: %v", err)
	}
}

// TestTheShapeIsHeldWithTheQuota. Letting the shape through while holding the
// quota is the half-applied state this area keeps producing: machines that
// exist and pods the quota refuses, reported as "namespace quota has no room
// for the replacement pod".
func TestTheShapeIsHeldWithTheQuota(t *testing.T) {
	folderTree(t, map[string]string{
		"tight2": `{"parent_id": null, "quota": {"cpu": "1"}}`,
	})
	mustTenant(t, tenantIn("tcs", "tight2"))

	eventually(t, "the workers being held", func() error {
		c := condition(t, "tcs", platformv1alpha1.ConditionWorkersReady)
		if c == nil || c.Reason != "HeldByFolderQuota" {
			return fmt.Errorf("WorkersReady = %v", c)
		}
		return nil
	})
}

// TestATenantThatFitsIsReserved — the same wiring has to say yes, and say so
// on the condition that was documented for it and never written.
func TestATenantThatFitsIsReserved(t *testing.T) {
	folderTree(t, map[string]string{
		"roomy": `{"parent_id": null, "quota": {"cpu": "100", "memory": "500Gi", "storage": "5000Gi"}}`,
	})
	mustTenant(t, tenantIn("tcr", "roomy"))

	eventually(t, "the reservation", func() error {
		c := condition(t, "tcr", platformv1alpha1.ConditionQuotaReserved)
		if c == nil || c.Status != metav1.ConditionTrue {
			return fmt.Errorf("QuotaReserved = %v", c)
		}
		if _, err := readQuota("tenant-tcr-quota", "tenant-tcr"); err != nil {
			return err
		}
		return nil
	})
}

// TestAFolderTheTreeDoesNotKnowIsNotRefused. Not knowing is not a no: an
// install with no folders, or a folder that predates the ConfigMap, must go on
// working exactly as it did.
func TestAFolderTheTreeDoesNotKnowIsNotRefused(t *testing.T) {
	folderTree(t, map[string]string{
		"elsewhere": `{"parent_id": null, "quota": {"cpu": "1"}}`,
	})
	mustTenant(t, tenantIn("tcu", "stranger"))

	eventually(t, "the reservation", func() error {
		c := condition(t, "tcu", platformv1alpha1.ConditionQuotaReserved)
		if c == nil || c.Status != metav1.ConditionTrue {
			return fmt.Errorf("QuotaReserved = %v", c)
		}
		return nil
	})
}

// TestAnExistingTenantIsNotFrozenByAnOverCommittedFolder.
//
// The way out of an over-committed folder is down, and a reconciler that
// refused everything while the folder was over its ceiling would be a locked
// door with no handle on either side. A reservation no larger than what the
// namespace already holds is not a growth and is never refused for room.
func TestAnExistingTenantIsNotFrozenByAnOverCommittedFolder(t *testing.T) {
	folderTree(t, map[string]string{
		"crowded": `{"parent_id": null, "quota": {"cpu": "1"}}`,
	})
	// The namespace the tenant will adopt, already holding far more than the
	// folder's ceiling — the state a lowered quota or a late-joining namespace
	// leaves behind.
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{
		Name:   "tenant-tce",
		Labels: map[string]string{"kubevirt-ui.io/folder": "crowded"},
	}}
	if err := k8sClient.Create(testCtx, ns); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating the namespace: %v", err)
	}
	planted := &corev1.ResourceQuota{
		ObjectMeta: metav1.ObjectMeta{Namespace: "tenant-tce", Name: "tenant-tce-quota"},
		Spec: corev1.ResourceQuotaSpec{Hard: corev1.ResourceList{
			corev1.ResourceRequestsCPU:     resource.MustParse("64"),
			corev1.ResourceRequestsMemory:  resource.MustParse("64Gi"),
			corev1.ResourceRequestsStorage: resource.MustParse("640Gi"),
		}},
	}
	if err := k8sClient.Create(testCtx, planted); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("planting the quota: %v", err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, planted) })

	mustTenant(t, tenantIn("tce", "crowded"))

	eventually(t, "the tenant reconciling anyway", func() error {
		c := condition(t, "tce", platformv1alpha1.ConditionQuotaReserved)
		if c == nil || c.Status != metav1.ConditionTrue {
			return fmt.Errorf("QuotaReserved = %v", c)
		}
		// And the quota came down to what the tenant actually reserves.
		quota, err := readQuota("tenant-tce-quota", "tenant-tce")
		if err != nil {
			return err
		}
		cpu := quota.Spec.Hard[corev1.ResourceRequestsCPU]
		if cpu.Cmp(resource.MustParse("64")) >= 0 {
			return fmt.Errorf("cpu is still %s", cpu.String())
		}
		return nil
	})
}
