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

package kube

// MergeSpec lays what this renders over what is already there, leaving
// everything it does not render alone.
//
// Necessary because the API server and Flux write their own defaults back into
// the object: a HelmRelease acquires `chart.spec.reconcileStrategy` that
// nothing here sets, and replacing the spec wholesale would strip it on every
// pass and write it back on every reconcile — the same loop kube-ovn's route
// defaults produce, in a different object.
//
// Deep, because the defaults arrive inside nested maps rather than at the top.
// Lists are replaced rather than merged: an addon's values are a statement of
// what the release should be, and half of somebody else's list is not a value
// anybody chose.
func MergeSpec(live, want map[string]any) map[string]any {
	out := map[string]any{}
	for key, value := range live {
		out[key] = value
	}
	for key, wanted := range want {
		if nested, ok := wanted.(map[string]any); ok {
			if existing, ok := out[key].(map[string]any); ok {
				out[key] = MergeSpec(existing, nested)
				continue
			}
		}
		out[key] = wanted
	}
	return out
}
