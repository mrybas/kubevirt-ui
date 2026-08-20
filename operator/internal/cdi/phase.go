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

// Package cdi renders and reads the CDI objects the operator manages.
package cdi

import (
	corev1 "k8s.io/api/core/v1"
	cdiv1 "kubevirt.io/containerized-data-importer-api/pkg/apis/core/v1beta1"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
)

// Interpret reduces a DataVolume to the product's four phases, and returns the
// message that explains a failure.
//
// This function is the single place that knows how to read CDI, because the
// backend learned the hard way that reading it in several places means reading
// it differently in several places — the same status was mapped in four files.
//
// The one rule nobody guesses right: CDI holds phase=Pending while it retries a
// failing import, and reports the failure only on the Running condition. Read
// the phase alone and a permanently broken import looks like a slow one,
// forever.
func Interpret(dv *cdiv1.DataVolume) (phase string, message string) {
	if dv == nil {
		return platformv1alpha1.ImagePhasePending, ""
	}

	for _, cond := range dv.Status.Conditions {
		if cond.Type != cdiv1.DataVolumeRunning || cond.Status != corev1.ConditionFalse {
			continue
		}
		if cond.Reason == "Error" || cond.Reason == "TransferFailed" {
			msg := cond.Message
			if msg == "" {
				msg = cond.Reason
			}
			return platformv1alpha1.ImagePhaseFailed, msg
		}
	}

	switch dv.Status.Phase {
	case cdiv1.Succeeded:
		return platformv1alpha1.ImagePhaseReady, ""
	case cdiv1.Failed:
		return platformv1alpha1.ImagePhaseFailed, "DataVolume reported phase Failed"
	case "":
		return platformv1alpha1.ImagePhasePending, ""
	default:
		// ImportScheduled, ImportInProgress, CloneScheduled, CloneInProgress,
		// Pending, WaitForFirstConsumer, Paused… all mean the same thing to the
		// product: it is working, keep waiting.
		return platformv1alpha1.ImagePhaseImporting, ""
	}
}
