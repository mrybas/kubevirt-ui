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
	"testing"

	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	cdiv1 "kubevirt.io/containerized-data-importer-api/pkg/apis/core/v1beta1"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
)

func newTemplate(ns, name, image string) *platformv1alpha1.ManagedVMTemplate {
	return &platformv1alpha1.ManagedVMTemplate{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns},
		Spec: platformv1alpha1.ManagedVMTemplateSpec{
			DisplayName: "OpDev Ubuntu",
			ImageRef:    platformv1alpha1.ImageRef{Name: image},
			Compute:     platformv1alpha1.TemplateComputeSpec{Cores: 2, Sockets: 1, Threads: 1, Memory: "4Gi"},
			RootDisk:    platformv1alpha1.TemplateRootDiskSpec{Size: "20Gi"},
		},
	}
}

func getTemplate(t *testing.T, ns, name string) *platformv1alpha1.ManagedVMTemplate {
	t.Helper()
	tpl := &platformv1alpha1.ManagedVMTemplate{}
	if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: name}, tpl); err != nil {
		t.Fatalf("reading template: %v", err)
	}
	return tpl
}

// A template pointing at nothing used to be possible and invisible: the
// reference was a generated DataVolume name inside a JSON blob, checked once at
// write time, and never again.
func TestATemplateSaysWhetherItsImageIsThere(t *testing.T) {
	ns := "tpl-dangling"
	mustNamespace(t, ns, "opdev")

	if err := k8sClient.Create(testCtx, newTemplate(ns, "ubuntu-base", "not-yet")); err != nil {
		t.Fatalf("creating template: %v", err)
	}

	eventually(t, "the dangling reference to be reported", func() error {
		got := getTemplate(t, ns, "ubuntu-base")
		cond := apimeta.FindStatusCondition(got.Status.Conditions, platformv1alpha1.ConditionImageFound)
		if cond == nil || cond.Reason != "ImageNotFound" {
			return fmt.Errorf("condition = %+v", cond)
		}
		return nil
	})

	// And it clears itself when the image turns up — templates and images may
	// be applied together, in either order.
	readyImage(t, ns, "not-yet")
	eventually(t, "the reference to resolve on its own", func() error {
		got := getTemplate(t, ns, "ubuntu-base")
		if !apimeta.IsStatusConditionTrue(got.Status.Conditions, platformv1alpha1.ConditionImageFound) {
			return fmt.Errorf("still unresolved")
		}
		if got.Status.ImageNamespace != ns {
			return fmt.Errorf("imageNamespace = %q, want the template's own", got.Status.ImageNamespace)
		}
		return nil
	})
}

func TestAVMTakesItsDefaultsFromTheTemplateResource(t *testing.T) {
	ns := "tpl-defaults"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")

	tpl := newTemplate(ns, "ubuntu-base", "ubuntu")
	tpl.Spec.Compute = platformv1alpha1.TemplateComputeSpec{Cores: 3, Sockets: 1, Threads: 1, Memory: "6Gi"}
	tpl.Spec.RootDisk = platformv1alpha1.TemplateRootDiskSpec{Size: "30Gi"}
	if err := k8sClient.Create(testCtx, tpl); err != nil {
		t.Fatalf("creating template: %v", err)
	}

	vm := &platformv1alpha1.ManagedVM{
		ObjectMeta: metav1.ObjectMeta{Name: "from-template", Namespace: ns},
		Spec: platformv1alpha1.ManagedVMSpec{
			DisplayName: "From template",
			TemplateRef: &platformv1alpha1.TemplateRef{Name: "ubuntu-base"},
			Running:     false,
		},
	}
	if err := k8sClient.Create(testCtx, vm); err != nil {
		t.Fatalf("creating vm: %v", err)
	}

	eventually(t, "the machine to carry the template's defaults", func() error {
		got, err := getKubeVirtVM(ns, "from-template")
		if err != nil {
			return err
		}
		domain := got.Spec.Template.Spec.Domain
		if domain.CPU.Cores != 3 {
			return fmt.Errorf("cores = %d, want 3", domain.CPU.Cores)
		}
		if domain.Memory.Guest.String() != "6Gi" {
			return fmt.Errorf("memory = %s, want 6Gi", domain.Memory.Guest)
		}
		size := got.Spec.DataVolumeTemplates[0].Spec.Storage.Resources.Requests["storage"]
		if size.String() != "30Gi" {
			return fmt.Errorf("disk = %s, want 30Gi", size.String())
		}
		if got.Labels["kubevirt-ui.io/template"] != "ubuntu-base" {
			return fmt.Errorf("template label = %q", got.Labels["kubevirt-ui.io/template"])
		}
		return nil
	})
}

// A template is a convenience, not a way around the rules: the image it names
// goes through the same readiness gate a direct reference does.
func TestATemplateDoesNotSkipTheImageReadinessGate(t *testing.T) {
	ns := "tpl-gate"
	mustNamespace(t, ns, "opdev")

	img := newImage(ns, "half-done")
	if err := k8sClient.Create(testCtx, img); err != nil {
		t.Fatalf("creating image: %v", err)
	}
	setDVStatus(t, ns, "half-done", cdiv1.ImportInProgress, nil, "10.0%")

	if err := k8sClient.Create(testCtx, newTemplate(ns, "half-base", "half-done")); err != nil {
		t.Fatalf("creating template: %v", err)
	}
	vm := &platformv1alpha1.ManagedVM{
		ObjectMeta: metav1.ObjectMeta{Name: "waiting", Namespace: ns},
		Spec: platformv1alpha1.ManagedVMSpec{
			TemplateRef: &platformv1alpha1.TemplateRef{Name: "half-base"},
		},
	}
	if err := k8sClient.Create(testCtx, vm); err != nil {
		t.Fatalf("creating vm: %v", err)
	}

	eventually(t, "the VM to wait on the template's unfinished image", func() error {
		got := getVM(t, ns, "waiting")
		cond := apimeta.FindStatusCondition(got.Status.Conditions, platformv1alpha1.ConditionImageReady)
		if cond == nil || cond.Reason != "ImageNotReady" {
			return fmt.Errorf("condition = %+v", cond)
		}
		return nil
	})
}
