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

// Package tenant sizes what a tenant cluster asks of its folder.
package tenant

import (
	"fmt"

	"k8s.io/apimachinery/pkg/api/resource"
)

// The control plane is counted because it genuinely spends the same namespace
// quota the workers do — its pods live in the tenant namespace. Leaving it out
// would not make it free; it would quietly eat the workers' headroom and show
// up as a worker that cannot start.
//
// The allowance is deliberately rough: six small containers per replica,
// defaulted to 50m/128Mi each by the namespace LimitRange.
const (
	cpPerReplicaMilliCPU = 500
	cpPerReplicaMemory   = 1 << 30
)

// What virt-launcher asks for beyond the guest's own memory.
//
// The terms mirror KubeVirt's own GetMemoryOverhead rather than a curve fitted
// to one cluster: a fit silently misses whatever the sample did not exercise,
// and drifts on a version bump with nothing to point at. Naming the parts makes
// a bump a diff instead of a mystery.
const (
	virtLauncherMonitorOverhead = 25 << 20
	virtLauncherOverhead        = 100 << 20
	virtlogdOverhead            = 20 << 20
	virtqemudOverhead           = 35 << 20
	qemuOverhead                = 30 << 20
	iothreadOverhead            = 8 << 20
	videoRAMOverhead            = 32 << 20
	perVCPUOverhead             = 8 << 20

	// Page tables are the term that makes this a formula rather than a
	// constant: they scale with guest memory, so a reserve chosen from a 2Gi
	// worker starves a 256Gi one — and only the largest tenants, the ones least
	// likely to be tested, would ever hit it.
	pagetableDivisor = 512

	// Over-reserving costs a slightly higher folder ceiling; under-reserving
	// reproduces the stall this was written for. The margin also absorbs
	// components a future KubeVirt adds before anyone reconciles the list.
	overheadNumerator   = 125
	overheadDenominator = 100

	// goldenSize is the shared Talos golden image, and the floor a worker root
	// clone can be.
	goldenSize = "20Gi"
)

// Sizing is what a tenant reserves.
type Sizing struct {
	Workers      int
	VCPU         int
	Memory       string
	Disk         string
	CPReplicas   int
	TalosWorkers bool
}

// Quota is the reservation, as Kubernetes quantities.
type Quota struct {
	CPU     resource.Quantity
	Memory  resource.Quantity
	Storage resource.Quantity
}

// VMIMemoryOverhead is what virt-launcher asks for beyond the guest's memory.
//
// Checked against what the pods really requested on the stand:
//
//	 2Gi / 2 vCPU  ->  2.273Gi   overhead 280Mi
//	 8Gi / 4 vCPU  ->  8.301Gi   overhead 308Mi
//	32Gi / 8 vCPU  -> 32.379Gi   overhead 388Mi
func VMIMemoryOverhead(memory int64, vcpu int) int64 {
	fixed := int64(virtLauncherMonitorOverhead + virtLauncherOverhead +
		virtlogdOverhead + virtqemudOverhead + qemuOverhead +
		iothreadOverhead + videoRAMOverhead)
	raw := fixed + memory/pagetableDivisor + int64(perVCPUOverhead*vcpu)
	return raw * overheadNumerator / overheadDenominator
}

// Reserve sizes the tenant from the request rather than measuring afterwards,
// so a folder ceiling can refuse one that does not fit before any of it exists.
func Reserve(s Sizing) (Quota, error) {
	// One worker of headroom, because replacing a worker overlaps with it.
	//
	// Sized to exactly the worker count, the quota refused every replacement:
	// remediation created the new Machine while the old one was still around
	// and the pod was rejected, so a tenant whose worker died could never get a
	// new one. The same slack is what lets a rolling resize proceed.
	surge := int64(s.Workers + 1)

	memoryPerWorker, err := bytesOf(s.Memory)
	if err != nil {
		return Quota{}, fmt.Errorf("worker memory: %w", err)
	}
	diskPerWorker, err := bytesOf(s.Disk)
	if err != nil {
		return Quota{}, fmt.Errorf("worker disk: %w", err)
	}

	cpuMilli := surge*int64(s.VCPU)*1000 + int64(s.CPReplicas)*cpPerReplicaMilliCPU
	memory := surge*(memoryPerWorker+VMIMemoryOverhead(memoryPerWorker, s.VCPU)) +
		int64(s.CPReplicas)*cpPerReplicaMemory
	storage := surge * diskPerWorker

	if s.TalosWorkers {
		// Each Talos worker clones its own root from the shared golden image,
		// and both are real PVCs. Counting only the worker disk left the quota
		// one clone short of the cluster it was provisioning, which reads as a
		// storage failure and is arithmetic.
		//
		// The golden image itself is deliberately *not* counted: it lives in a
		// shared namespace, and reserving 20Gi against a tenant for something
		// that is not in its namespace is the same defect with the sign
		// reversed.
		root, err := largerOf(s.Disk, goldenSize)
		if err != nil {
			return Quota{}, err
		}
		storage += surge * root
	}

	return Quota{
		CPU:     *resource.NewMilliQuantity(cpuMilli, resource.DecimalSI),
		Memory:  *resource.NewQuantity(memory, resource.BinarySI),
		Storage: *resource.NewQuantity(storage, resource.BinarySI),
	}, nil
}

// largerOf is the bigger of two quantities. The golden size is the one that
// must not be undercut, so it wins a tie and an unparseable input.
func largerOf(a, b string) (int64, error) {
	floor, err := bytesOf(b)
	if err != nil {
		return 0, err
	}
	value, err := bytesOf(a)
	if err != nil || value < floor {
		return floor, nil
	}
	return value, nil
}

func bytesOf(quantity string) (int64, error) {
	parsed, err := resource.ParseQuantity(quantity)
	if err != nil {
		return 0, fmt.Errorf("%q is not a quantity: %w", quantity, err)
	}
	return parsed.Value(), nil
}

// LargerSize is the bigger of two Kubernetes quantities, falling back to the
// second when either cannot be read.
//
// The fallback direction matters: this decides a clone's size against the image
// it is cloned from, and CDI refuses a clone smaller than its source at
// admission — which would fail every worker with an error nobody would connect
// to a disk-size field.
func LargerSize(asked, floor string) string {
	askedQ, err := resource.ParseQuantity(asked)
	if err != nil {
		return floor
	}
	floorQ, err := resource.ParseQuantity(floor)
	if err != nil {
		return floor
	}
	if askedQ.Cmp(floorQ) >= 0 {
		return asked
	}
	return floor
}
