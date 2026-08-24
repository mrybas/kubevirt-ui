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

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
)

const templateControllerName = "managedvmtemplate"

// ManagedVMTemplateReconciler keeps a template's status honest about the image
// it points at.
//
// The controller creates nothing. A template is data, and the only thing worth
// reconciling about data is whether the thing it references is there — which is
// exactly what nobody could see before, because the reference was a generated
// DataVolume name in a JSON blob and the validation happened once, at write
// time, in an HTTP handler.
type ManagedVMTemplateReconciler struct {
	client.Client
	Scheme   *runtime.Scheme
	Recorder record.EventRecorder
}

// +kubebuilder:rbac:groups=platform.kubevirt-ui.io,resources=managedvmtemplates,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=platform.kubevirt-ui.io,resources=managedvmtemplates/status,verbs=get;update;patch

// Reconcile resolves the template's image reference and reports it.
func (r *ManagedVMTemplateReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	tpl := &platformv1alpha1.ManagedVMTemplate{}
	if err := r.Get(ctx, req.NamespacedName, tpl); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if tpl.Annotations[pausedAnnotation] == "true" || !tpl.DeletionTimestamp.IsZero() {
		return ctrl.Result{}, nil
	}

	before := tpl.DeepCopy()

	ns := tpl.Spec.ImageRef.Namespace
	if ns == "" {
		ns = tpl.Namespace
	}
	tpl.Status.ImageNamespace = ns

	img := &platformv1alpha1.ManagedImage{}
	err := r.Get(ctx, types.NamespacedName{Namespace: ns, Name: tpl.Spec.ImageRef.Name}, img)
	switch {
	case err == nil:
		apimeta.SetStatusCondition(&tpl.Status.Conditions, metav1.Condition{
			Type:               platformv1alpha1.ConditionImageFound,
			Status:             metav1.ConditionTrue,
			Reason:             "Resolved",
			Message:            fmt.Sprintf("points at ManagedImage %s/%s", ns, tpl.Spec.ImageRef.Name),
			ObservedGeneration: tpl.Generation,
		})
	case apierrors.IsNotFound(err):
		// Not an error and not permanent: a template may be applied alongside
		// the image it names, in either order.
		apimeta.SetStatusCondition(&tpl.Status.Conditions, metav1.Condition{
			Type:               platformv1alpha1.ConditionImageFound,
			Status:             metav1.ConditionFalse,
			Reason:             "ImageNotFound",
			Message:            fmt.Sprintf("ManagedImage %s/%s does not exist", ns, tpl.Spec.ImageRef.Name),
			ObservedGeneration: tpl.Generation,
		})
	default:
		return ctrl.Result{}, fmt.Errorf("reading ManagedImage %s/%s: %w", ns, tpl.Spec.ImageRef.Name, err)
	}

	tpl.Status.ObservedGeneration = tpl.Generation
	if err := kube.UpdateStatus(ctx, r.Client, templateControllerName, tpl, before); err != nil {
		return ctrl.Result{}, fmt.Errorf("updating status: %w", err)
	}
	return ctrl.Result{}, nil
}

// SetupWithManager wires the controller.
func (r *ManagedVMTemplateReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&platformv1alpha1.ManagedVMTemplate{}).
		Watches(&platformv1alpha1.ManagedImage{},
			handler.EnqueueRequestsFromMapFunc(r.mapImageToTemplates)).
		Named(templateControllerName).
		Complete(r)
}

func (r *ManagedVMTemplateReconciler) mapImageToTemplates(
	ctx context.Context, obj client.Object,
) []reconcile.Request {
	list := &platformv1alpha1.ManagedVMTemplateList{}
	if err := r.List(ctx, list); err != nil {
		return nil
	}
	var out []reconcile.Request
	for i := range list.Items {
		tpl := &list.Items[i]
		ns := tpl.Spec.ImageRef.Namespace
		if ns == "" {
			ns = tpl.Namespace
		}
		if ns == obj.GetNamespace() && tpl.Spec.ImageRef.Name == obj.GetName() {
			out = append(out, reconcile.Request{
				NamespacedName: types.NamespacedName{Namespace: tpl.Namespace, Name: tpl.Name},
			})
		}
	}
	return out
}
