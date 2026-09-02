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

package cdi

import (
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
)

func registryImage(src platformv1alpha1.RegistrySource) *platformv1alpha1.ManagedImage {
	return &platformv1alpha1.ManagedImage{
		ObjectMeta: metav1.ObjectMeta{Name: "ubuntu", Namespace: "tenant-a"},
		Spec: platformv1alpha1.ManagedImageSpec{
			Source: platformv1alpha1.ManagedImageSource{Registry: &src},
			Size:   "10Gi",
		},
	}
}

// A registry pull that carries no credential is anonymous, and anonymous is
// exactly what a private Harbor project refuses. Rendering only the URL was
// indistinguishable — in the CR, in the DataVolume, and in this function —
// from an image that legitimately needs no credential, so the whole failure
// surfaced much later inside CDI as an import error that never named one.
func TestARegistryPullCarriesItsCredentialThrough(t *testing.T) {
	source, err := Source(registryImage(platformv1alpha1.RegistrySource{
		URL:           "docker://harbor.example/vm-images-tenant-a/ubuntu:1",
		SecretRef:     "harbor-robot",
		CertConfigMap: "harbor-ca",
	}))
	if err != nil {
		t.Fatalf("Source: %v", err)
	}

	registry := source.Registry
	if registry == nil {
		t.Fatal("no registry source rendered")
	}
	if registry.SecretRef == nil {
		t.Fatal("secretRef dropped: the pull would be anonymous and a private project would refuse it")
	}
	if *registry.SecretRef != "harbor-robot" {
		t.Errorf("secretRef = %q, want harbor-robot", *registry.SecretRef)
	}
	if registry.CertConfigMap == nil {
		t.Fatal("certConfigMap dropped: a Harbor behind a private CA would fail the TLS handshake")
	}
	if *registry.CertConfigMap != "harbor-ca" {
		t.Errorf("certConfigMap = %q, want harbor-ca", *registry.CertConfigMap)
	}
}

// The opposite has to stay true as well. CDI refuses an import outright when
// certConfigMap names nothing, so an empty string must render as absent rather
// than as a pointer to "".
func TestAnUnsetCredentialIsAbsentRatherThanEmpty(t *testing.T) {
	source, err := Source(registryImage(platformv1alpha1.RegistrySource{
		URL: "docker://quay.io/org/image:tag",
	}))
	if err != nil {
		t.Fatalf("Source: %v", err)
	}

	if source.Registry.SecretRef != nil {
		t.Errorf("secretRef = %q, want absent", *source.Registry.SecretRef)
	}
	if source.Registry.CertConfigMap != nil {
		t.Errorf("certConfigMap = %q, want absent", *source.Registry.CertConfigMap)
	}
	if source.Registry.URL == nil || *source.Registry.URL != "docker://quay.io/org/image:tag" {
		t.Error("the URL itself must be unaffected")
	}
}
