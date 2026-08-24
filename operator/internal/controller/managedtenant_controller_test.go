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
	"fmt"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/tenant"
)

func mustTenant(t *testing.T, obj *platformv1alpha1.ManagedTenant) *platformv1alpha1.ManagedTenant {
	t.Helper()
	if err := k8sClient.Create(testCtx, obj); err != nil {
		t.Fatalf("creating tenant %s: %v", obj.Name, err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, obj) })
	return obj
}

func getTenant(t *testing.T, name string) *platformv1alpha1.ManagedTenant {
	t.Helper()
	out := &platformv1alpha1.ManagedTenant{}
	var last error
	for i := 0; i < 50; i++ {
		last = k8sClient.Get(testCtx, types.NamespacedName{Name: name}, out)
		if last == nil {
			return out
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("reading tenant %s: %v", name, last)
	return nil
}

func tenantCondition(obj *platformv1alpha1.ManagedTenant, kind string) *metav1.Condition {
	return apimeta.FindStatusCondition(obj.Status.Conditions, kind)
}

func readQuota(name, namespace string) (*corev1.ResourceQuota, error) {
	out := &corev1.ResourceQuota{}
	err := k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: namespace, Name: name,
	}, out)
	return out, err
}

func plainTenant(name string) *platformv1alpha1.ManagedTenant {
	return &platformv1alpha1.ManagedTenant{
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Spec: platformv1alpha1.ManagedTenantSpec{
			DisplayName: name, Folder: "poc", Environment: "dev",
			KubernetesVersion:    "v1.33.1",
			ControlPlaneReplicas: 2,
			Workers: platformv1alpha1.TenantWorkers{
				Count: 2, VCPU: 2, Memory: "2Gi", Disk: "20Gi", OS: "cloud-init",
			},
			Storage: platformv1alpha1.TenantStorage{AllowanceGi: ptr.To[int32](100), PVCCount: ptr.To[int32](20)},
		},
	}
}

// TestTheNamespaceCarriesWhatTheControlPlaneNeeds. The pod-security labels are
// not decoration: Kamaji's control-plane pods and virt-launcher both need
// privileges the restricted profile refuses, and a namespace without them
// admits neither.
func TestTheNamespaceCarriesWhatTheControlPlaneNeeds(t *testing.T) {
	mustTenant(t, plainTenant("tns"))

	eventually(t, "the namespace", func() error {
		ns := &corev1.Namespace{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "tenant-tns"}, ns); err != nil {
			return err
		}
		for key, want := range map[string]string{
			"pod-security.kubernetes.io/enforce": "privileged",
			"kubevirt-ui.io/tenant":              "tns",
			"kubevirt-ui.io/folder":              "poc",
			"kubevirt-ui.io/environment":         "dev",
			// Without this the tenant's worker VMs are invisible in the main
			// list to everyone but an admin.
			"kubevirt-ui.io/enabled": "true",
		} {
			if ns.Labels[key] != want {
				return fmt.Errorf("%s = %q, want %q", key, ns.Labels[key], want)
			}
		}
		return nil
	})
}

// TestTheLimitRangeIsThereBeforeTheQuotaBites.
//
// A quota on requests makes requests mandatory, and Kamaji's control-plane
// containers declare none. Every tenant created in a folder simply had no
// control plane: the TenantControlPlane sat NotReady with zero pods and the
// page said Provisioning forever.
func TestTheLimitRangeIsThereBeforeTheQuotaBites(t *testing.T) {
	mustTenant(t, plainTenant("tlr"))

	eventually(t, "the LimitRange and the quota", func() error {
		limits := &corev1.LimitRange{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: "tenant-tlr", Name: "tenant-tlr-limits",
		}, limits); err != nil {
			return err
		}
		if len(limits.Spec.Limits) != 1 {
			return fmt.Errorf("limits = %v", limits.Spec.Limits)
		}
		item := limits.Spec.Limits[0]
		if item.DefaultRequest.Cpu().String() != "50m" ||
			item.DefaultRequest.Memory().String() != "128Mi" {
			return fmt.Errorf("defaults = %v", item.DefaultRequest)
		}
		// A defaulted *limit* would throttle the apiserver at whatever number
		// was picked here, so there must not be one.
		if len(item.Default) != 0 {
			return fmt.Errorf("a default limit was set: %v", item.Default)
		}
		if _, err := readQuota("tenant-tlr-quota", "tenant-tlr"); err != nil {
			return err
		}
		return nil
	})
}

// TestTheQuotaCapsRequestsAndNeverLimits. A ResourceQuota that caps a limit
// makes the API server require that limit on every pod in the namespace.
func TestTheQuotaCapsRequestsAndNeverLimits(t *testing.T) {
	mustTenant(t, plainTenant("tql"))

	eventually(t, "the quota", func() error {
		quota, err := readQuota("tenant-tql-quota", "tenant-tql")
		if err != nil {
			return err
		}
		for name := range quota.Spec.Hard {
			if strings.HasPrefix(string(name), "limits.") {
				return fmt.Errorf("the quota caps %s", name)
			}
		}
		if _, ok := quota.Spec.Hard[corev1.ResourceRequestsCPU]; !ok {
			return fmt.Errorf("no cpu request cap: %v", quota.Spec.Hard)
		}
		return nil
	})
}

// TestOneObjectHoldsBothStorageIntents.
//
// The workers' disks and the tenant's own workload allowance both spend
// `requests.storage` in one namespace. As two objects, Kubernetes enforces the
// smaller and the folder ceiling — which sums every quota it finds — charges
// for both: measured on the stand as a tenant charged 220Gi and able to use
// 100. One object, summed, makes the charge and the permission agree.
func TestOneObjectHoldsBothStorageIntents(t *testing.T) {
	// Talos, so the machines cost storage at all: a cloud-init pool boots a
	// containerDisk onto an emptyDisk and asks for no PVC storage, which would
	// make "summed" and "allowance only" the same number.
	mustTenant(t, talosTenant("tsum"))

	eventually(t, "the summed quota", func() error {
		quota, err := readQuota("tenant-tsum-quota", "tenant-tsum")
		if err != nil {
			return err
		}
		storage := quota.Spec.Hard[corev1.ResourceRequestsStorage]
		// Three root clones of 20Gi, plus the 100Gi allowance.
		want := resource.NewQuantity(3*int64(20<<30)+int64(100<<30), resource.BinarySI)
		if storage.Cmp(*want) != 0 {
			return fmt.Errorf("storage = %s, want %s", storage.String(), want.String())
		}
		pvcs := quota.Spec.Hard[corev1.ResourcePersistentVolumeClaims]
		if pvcs.Value() != 20 {
			return fmt.Errorf("pvc count = %s", pvcs.String())
		}
		return nil
	})

	eventually(t, "the reservation to be published", func() error {
		obj := getTenant(t, "tsum")
		if obj.Status.Reservation == nil {
			return fmt.Errorf("no reservation")
		}
		cond := tenantCondition(obj, platformv1alpha1.ConditionQuotaReserved)
		if cond == nil || cond.Status != metav1.ConditionTrue {
			return fmt.Errorf("QuotaReserved = %v", cond)
		}
		return nil
	})
}

// TestAnotherWritersQuotaIsNotMadeWorse.
//
// Adding the allowance while somebody else's storage cap is still there would
// take the folder charge higher and change nothing about what is enforced — the
// other object still binds at its own number. So this writes the machines only
// and says what is wrong, rather than making it worse.
func TestAnotherWritersQuotaIsNotMadeWorse(t *testing.T) {
	mustTenant(t, talosTenant("tother"))

	eventually(t, "the namespace", func() error {
		ns := &corev1.Namespace{}
		return k8sClient.Get(testCtx, types.NamespacedName{Name: "tenant-tother"}, ns)
	})

	foreign := &corev1.ResourceQuota{
		ObjectMeta: metav1.ObjectMeta{Name: "tenant-storage", Namespace: "tenant-tother"},
		Spec: corev1.ResourceQuotaSpec{Hard: corev1.ResourceList{
			corev1.ResourceRequestsStorage:        resource.MustParse("100Gi"),
			corev1.ResourcePersistentVolumeClaims: resource.MustParse("20"),
		}},
	}
	if err := k8sClient.Create(testCtx, foreign); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("planting the other quota: %v", err)
	}

	eventually(t, "the machines-only quota and the explanation", func() error {
		quota, err := readQuota("tenant-tother-quota", "tenant-tother")
		if err != nil {
			return err
		}
		storage := quota.Spec.Hard[corev1.ResourceRequestsStorage]
		want := resource.NewQuantity(3*int64(20<<30), resource.BinarySI)
		if storage.Cmp(*want) != 0 {
			return fmt.Errorf("storage = %s, want the machines only (%s)",
				storage.String(), want.String())
		}
		// And no second PVC cap either.
		if _, ok := quota.Spec.Hard[corev1.ResourcePersistentVolumeClaims]; ok {
			return fmt.Errorf("a second PVC cap was added: %v", quota.Spec.Hard)
		}

		obj := getTenant(t, "tother")
		cond := tenantCondition(obj, platformv1alpha1.ConditionQuotaReserved)
		if cond == nil || cond.Reason != "CountedTwice" {
			return fmt.Errorf("condition = %v", cond)
		}
		if !strings.Contains(cond.Message, "tenant-storage") {
			return fmt.Errorf("the message does not name it: %s", cond.Message)
		}
		return nil
	})

	// Left alone, because something else wrote it.
	consistently(t, "the other quota surviving", 3*time.Second, func() error {
		if _, err := readQuota("tenant-storage", "tenant-tother"); err != nil {
			return fmt.Errorf("it was removed: %w", err)
		}
		return nil
	})
}

// TestTheTenantNamespaceStaysOnTheClusterOverlay.
//
// The opposite of what this test used to assert, and the stand is what settled
// it. Stamping the tenant's VPC subnet on the namespace puts the *control
// plane* in the VPC as well, where it cannot resolve the datastore:
//
//	failed to connect to host=kamaji-postgres-rw.o0-cnpg.svc:
//	lookup … on 10.96.0.10:53: i/o timeout
//
// kine then never opens its socket, the apiserver dies on "error creating
// leases", and six containers crash-loop with nothing in any message naming the
// network. Only the worker launcher pods belong in the VPC, and they get there
// by the annotation on their own template.
func TestTheTenantNamespaceStaysOnTheClusterOverlay(t *testing.T) {
	obj := plainTenant("tnet")
	obj.Spec.Network = "uat-net-x"
	mustTenant(t, obj)

	eventually(t, "the namespace", func() error {
		ns := &corev1.Namespace{}
		return k8sClient.Get(testCtx, types.NamespacedName{Name: "tenant-tnet"}, ns)
	})
	ns := &corev1.Namespace{}
	if err := k8sReader.Get(testCtx, types.NamespacedName{Name: "tenant-tnet"}, ns); err != nil {
		t.Fatalf("reading the namespace: %v", err)
	}
	if got, found := ns.Annotations["ovn.kubernetes.io/logical_switch"]; found {
		t.Errorf("the namespace was pinned to %q, which takes the control "+
			"plane into the VPC with it", got)
	}

	// And the workers still cross into it, by their own template.
	if got := tenant.LogicalSwitchOf(obj); got != "uat-net-x-default" {
		t.Errorf("the worker switch is %q", got)
	}
}

// TestANamespaceTheOldVersionPinnedIsHealed.
//
// Ceasing to write the annotation does not undo it. A namespace stamped by the
// version that pinned it to the tenant's VPC keeps the stamp for ever, and its
// control plane stays unreachable after the upgrade that fixed the cause.
//
// Only our own stamp is removed. `ovn-default` is kube-ovn's claim and the
// value a healthy tenant namespace carries; taking that away would be the same
// mistake pointed the other way.
func TestANamespaceTheOldVersionPinnedIsHealed(t *testing.T) {
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{
		Name: "tenant-theal",
		Annotations: map[string]string{
			"ovn.kubernetes.io/logical_switch": "uat-net-h-default",
			"kubevirt-ui.io/note":              "somebody else's",
		},
	}}
	if err := k8sClient.Create(testCtx, ns); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating the namespace: %v", err)
	}

	obj := plainTenant("theal")
	obj.Spec.Network = "uat-net-h"
	mustTenant(t, obj)

	eventually(t, "the stamp to be lifted", func() error {
		live := &corev1.Namespace{}
		if err := k8sReader.Get(testCtx, types.NamespacedName{Name: "tenant-theal"}, live); err != nil {
			return err
		}
		if got, found := live.Annotations["ovn.kubernetes.io/logical_switch"]; found {
			return fmt.Errorf("still pinned to %q", got)
		}
		if live.Annotations["kubevirt-ui.io/note"] != "somebody else's" {
			return fmt.Errorf("it took somebody else's annotation with it: %v",
				live.Annotations)
		}
		return nil
	})

	// And a namespace carrying kube-ovn's own claim keeps it.
	other := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{
		Name:        "tenant-tkeep",
		Annotations: map[string]string{"ovn.kubernetes.io/logical_switch": "ovn-default"},
	}}
	if err := k8sClient.Create(testCtx, other); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating the namespace: %v", err)
	}
	keep := plainTenant("tkeep")
	keep.Spec.Network = "uat-net-h"
	mustTenant(t, keep)

	consistently(t, "kube-ovn's own claim to survive", 5*time.Second, func() error {
		live := &corev1.Namespace{}
		if err := k8sReader.Get(testCtx, types.NamespacedName{Name: "tenant-tkeep"}, live); err != nil {
			return err
		}
		if live.Annotations["ovn.kubernetes.io/logical_switch"] != "ovn-default" {
			return fmt.Errorf("annotations = %v", live.Annotations)
		}
		return nil
	})
}

// TestATenantWithoutStorageIsChargedForNone.
//
// Found by adopting the second live tenant. The first had storage and never
// asked the question; this one has no driver, no credential and no volumes, and
// the field's default would have charged its folder for a hundred gigabytes
// nothing could use.
//
// Zero is an answer. And a PVC cap of zero is not the way to say it — the
// workers' root disks are claims in this namespace, so a cap of zero refuses
// them and the tenant cannot replace a node.
func TestATenantWithoutStorageIsChargedForNone(t *testing.T) {
	obj := talosTenant("tnostore")
	obj.Spec.Storage = platformv1alpha1.TenantStorage{AllowanceGi: ptr.To[int32](0), PVCCount: ptr.To[int32](0)}
	mustTenant(t, obj)

	eventually(t, "the quota", func() error {
		quota, err := readQuota("tenant-tnostore-quota", "tenant-tnostore")
		if err != nil {
			return err
		}
		storage := quota.Spec.Hard[corev1.ResourceRequestsStorage]
		// The root clones and nothing else.
		want := resource.NewQuantity(3*int64(20<<30), resource.BinarySI)
		if storage.Cmp(*want) != 0 {
			return fmt.Errorf("storage = %s, want %s", storage.String(), want.String())
		}
		if _, capped := quota.Spec.Hard[corev1.ResourcePersistentVolumeClaims]; capped {
			return fmt.Errorf("it wrote a claim cap of zero, which refuses the " +
				"workers' own root disks")
		}
		return nil
	})
}
