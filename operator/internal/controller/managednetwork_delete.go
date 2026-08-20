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
	"sort"
	"strings"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
)

const (
	// networkFinalizer is added only to networks that asked to be cascaded.
	//
	// Absent by default, which is the whole point: a description of a network
	// somebody else built must be removable without consequence.
	networkFinalizer = "platform.kubevirt-ui.io/network"

	// deletionPolicyDelete opts in to the cascade.
	deletionPolicyDelete = "Delete"

	// drainRetry is how often a stuck teardown is looked at again. kube-ovn
	// takes seconds to finalize a subnet against its router, and the failure
	// mode being avoided is measured in hours.
	drainRetry = 5 * time.Second
)

func cascadeOnDelete(net *platformv1alpha1.ManagedNetwork) bool {
	return net.Spec.DeletionPolicy == deletionPolicyDelete
}

// reconcileFinalizer keeps the finalizer in step with the declared policy.
//
// Adding it on a Retain network would silently convert a note into an owner,
// and removing it from a Delete network would leave the cascade with nothing
// holding the object still long enough to run.
func (r *ManagedNetworkReconciler) reconcileFinalizer(
	ctx context.Context, net *platformv1alpha1.ManagedNetwork,
) (bool, error) {
	has := controllerutil.ContainsFinalizer(net, networkFinalizer)
	switch {
	case cascadeOnDelete(net) && !has:
		controllerutil.AddFinalizer(net, networkFinalizer)
	case !cascadeOnDelete(net) && has:
		controllerutil.RemoveFinalizer(net, networkFinalizer)
	default:
		return false, nil
	}
	if err := r.Update(ctx, net); err != nil {
		return false, fmt.Errorf("updating the finalizer: %w", err)
	}
	kube.CountWrite(r.Scheme, net, networkControllerName, "updated")
	return true, nil
}

// tearDown removes the network in the order kube-ovn requires, and refuses to
// take a shortcut when something is slow.
//
// Every subnet is finalized against the VPC's logical router, so deleting the
// router while they are still going strands them permanently: kube-ovn then
// loops on `not found logical router` and the finalizer never comes off. That
// was measured on this lab two hours after a delete — the subnet still
// terminating, and its addresses still missing from the pool.
//
// So the router goes last, and only once the subnets are actually gone. If they
// are not, this returns and comes back; it never forces.
func (r *ManagedNetworkReconciler) tearDown(
	ctx context.Context, net *platformv1alpha1.ManagedNetwork,
) (ctrl.Result, error) {
	before := net.DeepCopy()

	if !controllerutil.ContainsFinalizer(net, networkFinalizer) {
		// Retain: nothing was ever owned, so there is nothing to undo.
		return ctrl.Result{}, nil
	}

	if err := r.deleteIfPresent(ctx, vpcDNSGVK, net.Name+"-dns"); err != nil {
		return ctrl.Result{}, err
	}

	subnets, err := r.subnetsOf(ctx, net.Name)
	if err != nil {
		return ctrl.Result{}, err
	}
	for _, name := range subnets {
		if err := r.deleteIfPresent(ctx, subnetGVK, name); err != nil {
			return ctrl.Result{}, err
		}
	}

	// Read back rather than trust the delete call: the objects are marked, not
	// gone, and "marked" is exactly the state that must not be built on.
	stuck, err := r.subnetsOf(ctx, net.Name)
	if err != nil {
		return ctrl.Result{}, err
	}
	if len(stuck) > 0 {
		r.setNetworkCondition(net, platformv1alpha1.ConditionDeleting, false, "Draining",
			fmt.Sprintf("waiting for %s to finish deleting before removing the router; "+
				"kube-ovn finalizes them against it, and removing it first strands "+
				"them permanently", strings.Join(stuck, ", ")))
		if err := kube.UpdateStatus(ctx, r.Client, networkControllerName, net, before); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{RequeueAfter: drainRetry}, nil
	}

	if err := r.deleteIfPresent(ctx, vpcGVK, net.Name); err != nil {
		return ctrl.Result{}, err
	}

	controllerutil.RemoveFinalizer(net, networkFinalizer)
	if err := r.Update(ctx, net); err != nil {
		return ctrl.Result{}, fmt.Errorf("releasing the finalizer: %w", err)
	}
	kube.CountWrite(r.Scheme, net, networkControllerName, "updated")
	return ctrl.Result{}, nil
}

// subnetsOf lists the subnets that still exist on this VPC, deleting ones
// included: a subnet with a deletionTimestamp is still finalized against the
// router and still counts.
func (r *ManagedNetworkReconciler) subnetsOf(ctx context.Context, vpc string) ([]string, error) {
	list := &unstructured.UnstructuredList{}
	list.SetGroupVersionKind(subnetGVK.GroupVersion().WithKind("SubnetList"))
	if err := r.List(ctx, list); err != nil {
		return nil, fmt.Errorf("listing subnets: %w", err)
	}
	var out []string
	for i := range list.Items {
		owner, _, _ := unstructured.NestedString(list.Items[i].Object, "spec", "vpc")
		if owner == vpc {
			out = append(out, list.Items[i].GetName())
		}
	}
	sort.Strings(out)
	return out, nil
}

func (r *ManagedNetworkReconciler) deleteIfPresent(
	ctx context.Context, gvk schema.GroupVersionKind, name string,
) error {
	obj := &unstructured.Unstructured{}
	obj.SetGroupVersionKind(gvk)
	obj.SetName(name)
	if err := r.Get(ctx, types.NamespacedName{Name: name}, obj); err != nil {
		if apierrors.IsNotFound(err) {
			return nil
		}
		return fmt.Errorf("reading %s/%s: %w", gvk.Kind, name, err)
	}
	if !obj.GetDeletionTimestamp().IsZero() {
		// Already going. Asking again is a write that changes nothing.
		return nil
	}
	if err := kube.Delete(ctx, r.Client, networkControllerName, obj); err != nil {
		return fmt.Errorf("deleting %s/%s: %w", gvk.Kind, name, err)
	}
	return nil
}
