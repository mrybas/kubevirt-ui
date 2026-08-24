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

package tenant

import (
	"k8s.io/apimachinery/pkg/api/resource"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
)

// NamespaceOf is where a tenant's objects live. Derived, so nothing has to
// allocate or remember it.
func NamespaceOf(name string) string {
	return "tenant-" + name
}

// NamespaceLabels are what the namespace carries.
//
// The pod-security ones are not optional: Kamaji's control-plane pods and
// KubeVirt's virt-launcher both need privileges the restricted profile refuses,
// and a namespace without them admits neither.
func NamespaceLabels(obj *platformv1alpha1.ManagedTenant) map[string]string {
	labels := map[string]string{
		"kubevirt-ui.io/tenant":  obj.Name,
		"kubevirt-ui.io/managed": "true",
		// What makes the tenant's worker VMs visible in the main VM list to
		// users who have folder access. Without it they are invisible to
		// everyone but an admin.
		"kubevirt-ui.io/enabled":     "true",
		"kubevirt-ui.io/worker-type": "vm",

		"pod-security.kubernetes.io/enforce":         "privileged",
		"pod-security.kubernetes.io/enforce-version": "latest",
		"pod-security.kubernetes.io/warn":            "privileged",
		"pod-security.kubernetes.io/audit":           "privileged",
	}
	if obj.Spec.Folder != "" {
		labels["kubevirt-ui.io/folder"] = obj.Spec.Folder
	}
	if obj.Spec.Environment != "" {
		labels["kubevirt-ui.io/environment"] = obj.Spec.Environment
	}
	return labels
}

// LogicalSwitchOf is the tenant's VPC subnet, or "".
//
// For the worker launcher pods, which cross into it. Deliberately not for the
// namespace: the control plane lives there too and has to reach the datastore,
// the ingress and the rest of the platform, none of which a VPC can see.
func LogicalSwitchOf(obj *platformv1alpha1.ManagedTenant) string {
	if obj.Spec.Network == "" {
		return ""
	}
	return obj.Spec.Network + "-default"
}

// SizingOf is the tenant's request, in the shape the reservation takes.
func SizingOf(obj *platformv1alpha1.ManagedTenant) Sizing {
	workers := obj.Spec.Workers
	return Sizing{
		Workers:      int(orDefault32(workers.Count, 2)),
		VCPU:         int(orDefault32(workers.VCPU, 2)),
		Memory:       orDefaultString(workers.Memory, "2Gi"),
		Disk:         orDefaultString(workers.Disk, "20Gi"),
		CPReplicas:   int(orDefault32(obj.Spec.ControlPlaneReplicas, 2)),
		TalosWorkers: workers.OS == "talos",
	}
}

// WithStorageAllowance adds what the tenant's own workloads may provision.
//
// One number in one object. These used to be two ResourceQuotas in the same
// namespace: Kubernetes applies them independently, so the effective cap was
// the smaller, while the folder ceiling — which sums every quota it finds —
// charged the tenant for its storage twice.
func WithStorageAllowance(q Quota, allowanceBytes int64) Quota {
	total := q
	storage := q.Storage.DeepCopy()
	storage.Add(*resource.NewQuantity(allowanceBytes, resource.BinarySI))
	total.Storage = storage
	return total
}

func orDefaultString(value, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}

func orDefault32(value, fallback int32) int32 {
	if value == 0 {
		return fallback
	}
	return value
}
