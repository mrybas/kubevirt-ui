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

	"k8s.io/apimachinery/pkg/types"
	cdiv1 "kubevirt.io/containerized-data-importer-api/pkg/apis/core/v1beta1"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
)

// A private registry pull needs a credential, and the credential has to survive
// being stored.
//
// The backend built a source carrying `secretRef`, every check passed, the CR
// was written — and the API server PRUNED the field, because the CRD is a
// structural schema and `RegistrySource` did not declare it. CDI then pulled
// anonymously and failed with a Harbor 401 that never mentioned a credential.
//
// The reason it went unnoticed is the shape of the test that covered it: a
// dict asserted against a mocked API server, which stores whatever it is
// handed. Only a real API server with the real CRD can answer this, so this
// test writes the object and reads it back — and then checks the DataVolume
// the controller renders from it, because a field that survives storage and is
// then dropped on the way to CDI fails in exactly the same silence.

func newRegistryImage(ns, name string) *platformv1alpha1.ManagedImage {
	img := newImage(ns, name)
	img.Spec.Source = platformv1alpha1.ManagedImageSource{
		Registry: &platformv1alpha1.RegistrySource{
			URL:           "docker://harbor.example.com/team/ubuntu:24.04",
			SecretRef:     "harbor-robot",
			CertConfigMap: "harbor-ca",
		},
	}
	return img
}

func TestARegistryCredentialSurvivesTheApiServer(t *testing.T) {
	ns := "img-registry-roundtrip"
	mustNamespace(t, ns, "opdev")

	if err := k8sClient.Create(testCtx, newRegistryImage(ns, "private-ubuntu")); err != nil {
		t.Fatalf("creating image: %v", err)
	}

	// Read back through the API server, not from the object we just built:
	// the whole failure was that those two disagree.
	live := &platformv1alpha1.ManagedImage{}
	eventually(t, "the stored image to keep its credential", func() error {
		if err := k8sClient.Get(testCtx,
			types.NamespacedName{Namespace: ns, Name: "private-ubuntu"}, live); err != nil {
			return err
		}
		if live.Spec.Source.Registry == nil {
			return fmt.Errorf("the registry source itself did not survive")
		}
		if got := live.Spec.Source.Registry.SecretRef; got != "harbor-robot" {
			return fmt.Errorf("secretRef = %q — pruned on write, so the pull is anonymous", got)
		}
		if got := live.Spec.Source.Registry.CertConfigMap; got != "harbor-ca" {
			return fmt.Errorf("certConfigMap = %q", got)
		}
		return nil
	})
}

func TestTheCredentialReachesTheDataVolume(t *testing.T) {
	ns := "img-registry-datavolume"
	mustNamespace(t, ns, "opdev")

	if err := k8sClient.Create(testCtx, newRegistryImage(ns, "private-ubuntu")); err != nil {
		t.Fatalf("creating image: %v", err)
	}

	eventually(t, "the DataVolume to carry the credential CDI will resolve", func() error {
		dv := &cdiv1.DataVolume{}
		if err := k8sClient.Get(testCtx,
			types.NamespacedName{Namespace: ns, Name: "private-ubuntu"}, dv); err != nil {
			return err
		}
		if dv.Spec.Source == nil || dv.Spec.Source.Registry == nil {
			return fmt.Errorf("no registry source on the DataVolume: %+v", dv.Spec.Source)
		}
		reg := dv.Spec.Source.Registry
		if reg.URL == nil || *reg.URL != "docker://harbor.example.com/team/ubuntu:24.04" {
			return fmt.Errorf("url = %v", reg.URL)
		}
		// CDI resolves both of these in the DataVolume's OWN namespace, which
		// is why they are names rather than values and why the Secret has to
		// exist here rather than centrally.
		if reg.SecretRef == nil || *reg.SecretRef != "harbor-robot" {
			return fmt.Errorf("secretRef = %v — CDI would pull anonymously", reg.SecretRef)
		}
		if reg.CertConfigMap == nil || *reg.CertConfigMap != "harbor-ca" {
			return fmt.Errorf("certConfigMap = %v", reg.CertConfigMap)
		}
		return nil
	})
}

func TestARegistryImageWithoutACredentialStaysAnonymous(t *testing.T) {
	// The other half. A public registry has no Secret, and asserting one that
	// does not exist is worse than asserting none: CDI refuses an import
	// outright when certConfigMap names a ConfigMap that is not there.
	ns := "img-registry-public"
	mustNamespace(t, ns, "opdev")

	img := newImage(ns, "public-ubuntu")
	img.Spec.Source = platformv1alpha1.ManagedImageSource{
		Registry: &platformv1alpha1.RegistrySource{
			URL: "docker://quay.io/containerdisks/ubuntu:24.04",
		},
	}
	if err := k8sClient.Create(testCtx, img); err != nil {
		t.Fatalf("creating image: %v", err)
	}

	eventually(t, "the DataVolume to name neither", func() error {
		dv := &cdiv1.DataVolume{}
		if err := k8sClient.Get(testCtx,
			types.NamespacedName{Namespace: ns, Name: "public-ubuntu"}, dv); err != nil {
			return err
		}
		if dv.Spec.Source == nil || dv.Spec.Source.Registry == nil {
			return fmt.Errorf("no registry source: %+v", dv.Spec.Source)
		}
		if ref := dv.Spec.Source.Registry.SecretRef; ref != nil && *ref != "" {
			return fmt.Errorf("secretRef = %q, invented for a public image", *ref)
		}
		if cm := dv.Spec.Source.Registry.CertConfigMap; cm != nil && *cm != "" {
			return fmt.Errorf("certConfigMap = %q, and CDI refuses an import that names a missing one", *cm)
		}
		return nil
	})
}
