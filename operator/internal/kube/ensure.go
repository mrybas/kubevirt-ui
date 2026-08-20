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

// Package kube holds the write path every controller shares.
//
// One rule lives here and nowhere else: we write only when something actually
// differs. Every controller goes through these helpers so the rule cannot be
// forgotten in one place while being honoured in another, and so the
// patches_total counter tells the truth about the whole operator.
package kube

import (
	"context"
	"reflect"

	"k8s.io/apimachinery/pkg/api/equality"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	"github.com/mrybas/kubevirt-ui/operator/internal/metrics"
)

// Ensure creates obj if missing, otherwise applies mutate and updates it only
// when mutate actually changed something.
//
// This is controllerutil.CreateOrUpdate plus the counter: the diff check is
// upstream's, the observability is ours.
func Ensure(
	ctx context.Context,
	c client.Client,
	controller string,
	obj client.Object,
	mutate controllerutil.MutateFn,
) (controllerutil.OperationResult, error) {
	res, err := controllerutil.CreateOrUpdate(ctx, c, obj, mutate)
	if err != nil {
		return res, err
	}
	switch res {
	case controllerutil.OperationResultCreated:
		count(c.Scheme(), obj, controller, "created")
	case controllerutil.OperationResultUpdated:
		count(c.Scheme(), obj, controller, "updated")
	case controllerutil.OperationResultNone,
		controllerutil.OperationResultUpdatedStatus,
		controllerutil.OperationResultUpdatedStatusOnly:
		// nothing written, or status handled by UpdateStatus below
	}
	return res, nil
}

// UpdateStatus writes obj's status subresource only when it differs from the
// status the object had when this reconcile pass read it.
//
// before must be the untouched copy taken at the top of Reconcile. Comparing
// whole objects would be wrong (spec may legitimately differ mid-pass), so only
// the Status field is compared.
func UpdateStatus(
	ctx context.Context,
	c client.Client,
	controller string,
	obj client.Object,
	before client.Object,
) error {
	if statusEqual(obj, before) {
		return nil
	}
	if err := c.Status().Update(ctx, obj); err != nil {
		return err
	}
	count(c.Scheme(), obj, controller, "status")
	return nil
}

// Delete removes obj and counts the write. A missing object is not a write and
// not an error — deletion is idempotent by nature.
func Delete(ctx context.Context, c client.Client, controller string, obj client.Object) error {
	err := c.Delete(ctx, obj)
	if err != nil {
		if client.IgnoreNotFound(err) == nil {
			return nil
		}
		return err
	}
	count(c.Scheme(), obj, controller, "deleted")
	return nil
}

func statusEqual(a, b client.Object) bool {
	av := reflect.ValueOf(a)
	bv := reflect.ValueOf(b)
	for av.Kind() == reflect.Ptr {
		av = av.Elem()
	}
	for bv.Kind() == reflect.Ptr {
		bv = bv.Elem()
	}
	if av.Kind() != reflect.Struct || bv.Kind() != reflect.Struct {
		return false
	}
	af := av.FieldByName("Status")
	bf := bv.FieldByName("Status")
	if !af.IsValid() || !bf.IsValid() {
		// No Status field to compare: never claim equality, let the caller write.
		return false
	}
	return equality.Semantic.DeepEqual(af.Interface(), bf.Interface())
}

// KindOf resolves the kind label for metrics. Unregistered types fall back to
// the Go type name — a metric with a slightly odd label beats a panic.
func KindOf(scheme *runtime.Scheme, obj client.Object) string {
	if gvk := obj.GetObjectKind().GroupVersionKind(); gvk.Kind != "" {
		return gvk.Kind
	}
	if scheme != nil {
		if gvks, _, err := scheme.ObjectKinds(obj); err == nil && len(gvks) > 0 {
			return gvks[0].Kind
		}
	}
	t := reflect.TypeOf(obj)
	for t != nil && t.Kind() == reflect.Ptr {
		t = t.Elem()
	}
	if t == nil {
		return "Unknown"
	}
	return t.Name()
}

// CountWrite records a write issued outside the helpers above — a plain Create
// where CreateOrUpdate would be wrong because the object must never be updated
// once it exists.
func CountWrite(scheme *runtime.Scheme, obj client.Object, controller, op string) {
	count(scheme, obj, controller, op)
}

func count(scheme *runtime.Scheme, obj client.Object, controller, op string) {
	metrics.PatchesTotal.WithLabelValues(KindOf(scheme, obj), controller, op).Inc()
}
