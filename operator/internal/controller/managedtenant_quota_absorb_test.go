package controller

import (
	"fmt"
	"testing"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
)

// TestTheQuotaTheBackendUsedToWriteIsAbsorbed.
//
// `QuotaReserved=False (CountedTwice)` has been on every tenant with storage
// since the handover, and the explanation was right: two quotas cap the same
// namespace, Kubernetes enforces the smaller, and the folder ceiling sums both
// and charges for the storage twice.
//
// What was wrong was treating the second one as somebody else's. It is
// `tenant-storage`, written by this product's own backend when it wires a
// tenant's CSI access — the older writer, still writing. So the newer one
// takes it over: the summed quota is written first, which changes nothing
// while both exist because the smaller still binds, and then the predecessor
// is removed, which widens the cap to the figure the folder was already being
// charged for.
//
// A condition that is always false teaches people to stop reading conditions.
func TestTheQuotaTheBackendUsedToWriteIsAbsorbed(t *testing.T) {
	ns := "tenant-absorb"
	mustTenant(t, talosTenant("absorb"))

	// The backend's object, as it exists on the stand today.
	legacy := &corev1.ResourceQuota{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: ns, Name: "tenant-storage",
			Labels: map[string]string{
				"kubevirt-ui.io/managed": "true",
				"kubevirt-ui.io/role":    "csi-infra",
				"kubevirt-ui.io/tenant":  "absorb",
			},
		},
		Spec: corev1.ResourceQuotaSpec{Hard: corev1.ResourceList{
			corev1.ResourceRequestsStorage:        resource.MustParse("100Gi"),
			corev1.ResourcePersistentVolumeClaims: resource.MustParse("20"),
		}},
	}
	eventually(t, "the namespace to exist", func() error {
		got := &corev1.Namespace{}
		return k8sClient.Get(testCtx, types.NamespacedName{Name: ns}, got)
	})
	if err := k8sClient.Create(testCtx, legacy); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("planting the legacy quota: %v", err)
	}
	// Nothing watches ResourceQuotas, so the pass that would notice has to be
	// asked for — and asked again, because the first nudge can be served from
	// a cache that has not seen the object yet. On a cluster the pass arrives
	// with the upgrade: a new operator reconciles everything it owns at
	// startup, which is the same moment the backend stops writing this.
	nudges := 0
	eventually(t, "it to be taken over", func() error {
		nudges++
		touchTenant(t, "absorb", nudges)
		old := &corev1.ResourceQuota{}
		err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: ns, Name: "tenant-storage",
		}, old)
		if err == nil {
			return fmt.Errorf("the predecessor is still there")
		}
		if !apierrors.IsNotFound(err) {
			return err
		}
		// And what replaced it carries the allowance, not just the machines.
		quota, err := readQuota(ns+"-quota", ns)
		if err != nil {
			return err
		}
		storage := quota.Spec.Hard[corev1.ResourceRequestsStorage]
		want := resource.NewQuantity(3*int64(20<<30)+int64(100<<30), resource.BinarySI)
		if storage.Cmp(*want) != 0 {
			return fmt.Errorf("storage = %s, want %s", storage.String(), want.String())
		}
		return nil
	})

	eventually(t, "the condition to stop crying wolf", func() error {
		cond := apimeta.FindStatusCondition(
			getTenant(t, "absorb").Status.Conditions,
			platformv1alpha1.ConditionQuotaReserved)
		if cond == nil || cond.Status != metav1.ConditionTrue {
			return fmt.Errorf("QuotaReserved = %v", cond)
		}
		return nil
	})
}

// TestSomebodyElsesQuotaIsStillLeftAlone.
//
// The reason the redundant-quota path exists at all: a cap somebody put there
// deliberately is not ours to remove. Only the three labels of our own older
// writer earn that, and the tenant says what it found.
func TestSomebodyElsesQuotaIsStillLeftAlone(t *testing.T) {
	ns := "tenant-foreign"
	mustTenant(t, talosTenant("foreign"))

	eventually(t, "the namespace to exist", func() error {
		got := &corev1.Namespace{}
		return k8sClient.Get(testCtx, types.NamespacedName{Name: ns}, got)
	})
	theirs := &corev1.ResourceQuota{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "platform-cap"},
		Spec: corev1.ResourceQuotaSpec{Hard: corev1.ResourceList{
			corev1.ResourceRequestsStorage: resource.MustParse("50Gi"),
		}},
	}
	if err := k8sClient.Create(testCtx, theirs); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("planting a foreign quota: %v", err)
	}
	touchTenant(t, "foreign", 1)

	eventually(t, "it to be reported and kept", func() error {
		got := getTenant(t, "foreign")
		cond := apimeta.FindStatusCondition(
			got.Status.Conditions, platformv1alpha1.ConditionQuotaReserved)
		if cond == nil || cond.Reason != "CountedTwice" {
			return fmt.Errorf("QuotaReserved = %v", cond)
		}
		still := &corev1.ResourceQuota{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: ns, Name: "platform-cap",
		}, still); err != nil {
			return fmt.Errorf("a quota this operator did not write was removed: %w", err)
		}
		return nil
	})
}

// touchTenant asks for a reconcile the way a restart would.
func touchTenant(t *testing.T, name string, n int) {
	t.Helper()
	obj := getTenant(t, name)
	if obj.Annotations == nil {
		obj.Annotations = map[string]string{}
	}
	obj.Annotations["test.kubevirt-ui.io/nudge"] = fmt.Sprintf("%s-%d", name, n)
	if err := k8sClient.Update(testCtx, obj); err != nil && !apierrors.IsConflict(err) {
		t.Fatalf("nudging %s: %v", name, err)
	}
}
