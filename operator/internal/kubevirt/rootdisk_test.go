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

package kubevirt

import (
	"testing"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
)

// The root disk names the image's DataSource when the image publishes one, and
// the claim when it does not.
//
// Both must keep working at once and for a long time: an installation upgrades
// its operator before its images have published anything, and a legacy template
// names a DataVolume that has no ManagedImage behind it at all. A renderer that
// only knew the new form would leave those machines with a disk pointing at
// nothing.
func rootDiskInput() Input {
	return Input{
		VM:           &platformv1alpha1.ManagedVM{},
		DiskSize:     "20Gi",
		RootDiskName: "web-01-root-1",
	}
}

func TestRootDiskNamesTheImagesDataSourceWhenThereIsOne(t *testing.T) {
	in := rootDiskInput()
	in.GoldenPVCName, in.GoldenPVCNamespace = "ubuntu-2404", "opdev-dev"
	in.GoldenDataSourceName, in.GoldenDataSourceNamespace = "ubuntu-2404", "opdev-dev"

	tpl, err := RootDiskTemplate(in)
	if err != nil {
		t.Fatalf("rendering: %v", err)
	}
	if tpl.Spec.SourceRef == nil {
		t.Fatalf("no sourceRef; source is %+v", tpl.Spec.Source)
	}
	if tpl.Spec.Source != nil {
		t.Errorf("both forms at once: %+v", tpl.Spec.Source)
	}
	if tpl.Spec.SourceRef.Kind != "DataSource" || tpl.Spec.SourceRef.Name != "ubuntu-2404" {
		t.Errorf("sourceRef = %+v", tpl.Spec.SourceRef)
	}
	if tpl.Spec.SourceRef.Namespace == nil || *tpl.Spec.SourceRef.Namespace != "opdev-dev" {
		t.Errorf("sourceRef namespace = %v", tpl.Spec.SourceRef.Namespace)
	}
}

func TestRootDiskFallsBackToTheClaim(t *testing.T) {
	in := rootDiskInput()
	in.GoldenPVCName, in.GoldenPVCNamespace = "legacy-dv-abc12", "opdev-dev"

	tpl, err := RootDiskTemplate(in)
	if err != nil {
		t.Fatalf("rendering: %v", err)
	}
	if tpl.Spec.SourceRef != nil {
		t.Fatalf("named a DataSource that was never published: %+v", tpl.Spec.SourceRef)
	}
	if tpl.Spec.Source == nil || tpl.Spec.Source.PVC == nil {
		t.Fatalf("no claim source either: %+v", tpl.Spec)
	}
	if tpl.Spec.Source.PVC.Name != "legacy-dv-abc12" {
		t.Errorf("source claim = %q", tpl.Spec.Source.PVC.Name)
	}
}

// Whichever form is used, the disk's own size and storage settings are the
// tenant's, not the image's — the clone target is sized here.
func TestRootDiskKeepsItsOwnStorageInEitherForm(t *testing.T) {
	for _, form := range []string{"claim", "datasource"} {
		in := rootDiskInput()
		in.StorageClass = "ceph-block"
		in.GoldenPVCName, in.GoldenPVCNamespace = "ubuntu-2404", "opdev-dev"
		if form == "datasource" {
			in.GoldenDataSourceName, in.GoldenDataSourceNamespace = "ubuntu-2404", "opdev-dev"
		}
		tpl, err := RootDiskTemplate(in)
		if err != nil {
			t.Fatalf("%s: rendering: %v", form, err)
		}
		if tpl.Spec.Storage == nil {
			t.Fatalf("%s: no storage block", form)
		}
		if tpl.Spec.Storage.StorageClassName == nil ||
			*tpl.Spec.Storage.StorageClassName != "ceph-block" {
			t.Errorf("%s: storage class = %v", form, tpl.Spec.Storage.StorageClassName)
		}
		if got := tpl.Spec.Storage.Resources.Requests.Storage().String(); got != "20Gi" {
			t.Errorf("%s: size = %s", form, got)
		}
	}
}
