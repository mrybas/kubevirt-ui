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
	"fmt"
	"strconv"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	cdiv1 "kubevirt.io/containerized-data-importer-api/pkg/apis/core/v1beta1"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/naming"
)

// ImageLabels builds the label set a golden disk carries.
//
// These are exactly the keys the FastAPI listers filter on, so a disk created
// through the operator is visible in the UI without any change to the UI. The
// project label is only stamped for project-scoped images, matching the
// backend: a project label on an environment-scoped image would widen its
// visibility silently.
func ImageLabels(img *platformv1alpha1.ManagedImage, projectName string) map[string]string {
	slugSeed := img.Spec.DisplayName
	if slugSeed == "" {
		slugSeed = img.Name
	}

	labels := map[string]string{
		naming.ManagedLabel:    "true",
		naming.DiskTypeLabel:   img.Spec.DiskType,
		naming.PersistentLabel: strconv.FormatBool(img.Spec.Persistent),
		naming.SlugLabel:       naming.Slug(slugSeed),
		naming.ScopeLabel:      img.Spec.Scope,

		naming.OwnerUIDLabel:  string(img.UID),
		naming.OwnerNameLabel: img.Name,
		naming.OwnerKindLabel: "ManagedImage",
	}
	if img.Spec.Scope == "project" && projectName != "" {
		labels[naming.ProjectLabel] = projectName
	}
	if img.Spec.OSType != "" {
		labels[naming.OSTypeLabel] = img.Spec.OSType
	}
	if img.Spec.OSVersion != "" {
		labels[naming.OSVersionLabel] = img.Spec.OSVersion
	}
	return labels
}

// ImageAnnotations builds the annotation set a golden disk carries.
func ImageAnnotations(img *platformv1alpha1.ManagedImage) map[string]string {
	displayName := img.Spec.DisplayName
	if displayName == "" {
		displayName = img.Name
	}
	annotations := map[string]string{
		naming.DisplayNameAnnotation: displayName,
	}
	if img.Spec.Description != "" {
		annotations[naming.DescriptionAnnotation] = img.Spec.Description
	}
	return annotations
}

// Source translates our four-way source into CDI's.
func Source(img *platformv1alpha1.ManagedImage) (*cdiv1.DataVolumeSource, error) {
	s := img.Spec.Source
	switch {
	case s.HTTP != nil:
		return &cdiv1.DataVolumeSource{HTTP: &cdiv1.DataVolumeSourceHTTP{URL: s.HTTP.URL}}, nil
	case s.Registry != nil:
		url := s.Registry.URL
		registry := &cdiv1.DataVolumeSourceRegistry{URL: &url}
		// Carried through, not dropped. These two are what make a pull against
		// a private project work at all, and rendering only the URL is
		// indistinguishable — from here, from the CR, and from the DataVolume
		// — from an image that legitimately needs no credential. The failure
		// surfaces much later, inside CDI, as an import error that never names
		// a credential.
		if s.Registry.SecretRef != "" {
			secretRef := s.Registry.SecretRef
			registry.SecretRef = &secretRef
		}
		if s.Registry.CertConfigMap != "" {
			certConfigMap := s.Registry.CertConfigMap
			registry.CertConfigMap = &certConfigMap
		}
		return &cdiv1.DataVolumeSource{Registry: registry}, nil
	case s.PVC != nil:
		ns := s.PVC.Namespace
		if ns == "" {
			ns = img.Namespace
		}
		return &cdiv1.DataVolumeSource{PVC: &cdiv1.DataVolumeSourcePVC{Name: s.PVC.Name, Namespace: ns}}, nil
	case s.Blank != nil:
		return &cdiv1.DataVolumeSource{Blank: &cdiv1.DataVolumeBlankImage{}}, nil
	default:
		// Unreachable through the API server: the CEL rule on the source object
		// requires exactly one member. Reachable in unit tests, and a caller
		// that hits it deserves a name, not a nil dereference.
		return nil, fmt.Errorf("ManagedImage %s/%s has no source set", img.Namespace, img.Name)
	}
}

// DesiredDataVolume renders the disk for an image.
//
// volumeMode Block is not a preference: snapshot-based cloning needs it, and a
// filesystem-mode golden disk silently falls back to the slow host-assisted
// path for every VM cloned from it afterwards.
func DesiredDataVolume(
	img *platformv1alpha1.ManagedImage,
	projectName string,
) (*cdiv1.DataVolume, error) {
	source, err := Source(img)
	if err != nil {
		return nil, err
	}

	size, err := resource.ParseQuantity(img.Spec.Size)
	if err != nil {
		return nil, fmt.Errorf("size %q is not a quantity: %w", img.Spec.Size, err)
	}

	volumeMode := corev1.PersistentVolumeBlock
	storage := &cdiv1.StorageSpec{
		VolumeMode: &volumeMode,
		Resources: corev1.VolumeResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceStorage: size},
		},
	}
	if img.Spec.StorageClass != "" {
		sc := img.Spec.StorageClass
		storage.StorageClassName = &sc
	}

	return &cdiv1.DataVolume{
		ObjectMeta: metav1.ObjectMeta{
			// One-to-one child, so it carries the parent's name: nothing has to
			// be read back before it can be referenced, and status.dataVolumeName
			// stays the place code looks it up rather than a rule code guesses.
			Name:        img.Name,
			Namespace:   img.Namespace,
			Labels:      ImageLabels(img, projectName),
			Annotations: ImageAnnotations(img),
		},
		Spec: cdiv1.DataVolumeSpec{
			Source:  source,
			Storage: storage,
		},
	}, nil
}

// DesiredDataSource renders the named indirection to the finished disk.
//
// It exists so that KubeVirt-native consumers — and a future DataImportCron —
// can reference the image by a stable name without knowing this operator
// exists.
//
// snapshotName, when set, makes it point at the permanent snapshot instead of
// the claim. That is the whole of the cost difference on the consumer side:
// cloning from a claim makes CDI take a temporary snapshot per clone and throw
// it away (measured: one `tmp-snapshot-*` object per clone, median 7s against
// 3s from a snapshot on the same cluster), while cloning from a snapshot that
// already exists is a restore. Consumers reference this object either way and
// never learn which form is in effect.
func DesiredDataSource(
	img *platformv1alpha1.ManagedImage,
	projectName string,
	snapshotName string,
) *cdiv1.DataSource {
	source := cdiv1.DataSourceSource{
		PVC: &cdiv1.DataVolumeSourcePVC{
			Name:      img.Name,
			Namespace: img.Namespace,
		},
	}
	if snapshotName != "" {
		source = cdiv1.DataSourceSource{
			Snapshot: &cdiv1.DataVolumeSourceSnapshot{
				Name:      snapshotName,
				Namespace: img.Namespace,
			},
		}
	}
	return &cdiv1.DataSource{
		ObjectMeta: metav1.ObjectMeta{
			Name:        img.Name,
			Namespace:   img.Namespace,
			Labels:      ImageLabels(img, projectName),
			Annotations: ImageAnnotations(img),
		},
		Spec: cdiv1.DataSourceSpec{Source: source},
	}
}

// VolumeSnapshotGVK is the snapshot API this operator talks to unstructured.
// The external-snapshotter client is not a dependency of this module, and one
// object type does not justify becoming one.
var VolumeSnapshotGVK = schema.GroupVersionKind{
	Group: "snapshot.storage.k8s.io", Version: "v1", Kind: "VolumeSnapshot",
}

// SourceClaimUIDAnnotation records which claim the snapshot was taken from.
//
// Not the claim's name: names come back. The image's disk can be deleted and
// re-imported under the same name, and a snapshot of the previous volume stays
// perfectly usable afterwards — measured, it keeps cloning at the same speed —
// so nothing about it looks wrong while it serves content that no longer
// matches the image. The UID is what distinguishes the two, so it is what the
// controller compares before trusting the snapshot it finds.
const SourceClaimUIDAnnotation = "kubevirt-ui.io/source-claim-uid"

// SnapshotClassByDefault means "take the snapshot without naming a class". It
// is a sentinel rather than an empty string because empty already means "do not
// take a snapshot at all" on the calling side.
const SnapshotClassByDefault = "\x00default"

// DesiredVolumeSnapshot renders the permanent snapshot clones are taken from.
func DesiredVolumeSnapshot(
	img *platformv1alpha1.ManagedImage,
	projectName string,
	claimName, claimUID, snapshotClass string,
) *unstructured.Unstructured {
	snap := &unstructured.Unstructured{Object: map[string]any{}}
	snap.SetGroupVersionKind(VolumeSnapshotGVK)
	snap.SetName(img.Name)
	snap.SetNamespace(img.Namespace)
	snap.SetLabels(ImageLabels(img, projectName))

	annotations := map[string]string{}
	for k, v := range ImageAnnotations(img) {
		annotations[k] = v
	}
	annotations[SourceClaimUIDAnnotation] = claimUID
	snap.SetAnnotations(annotations)

	spec := map[string]any{
		"source": map[string]any{"persistentVolumeClaimName": claimName},
	}
	// No class named means the snapshot controller picks its default for the
	// driver — which is what CDI does too when the StorageProfile names none.
	// Naming one we chose ourselves would be a second opinion.
	if snapshotClass != "" && snapshotClass != SnapshotClassByDefault {
		spec["volumeSnapshotClassName"] = snapshotClass
	}
	_ = unstructured.SetNestedMap(snap.Object, spec, "spec")
	return snap
}
