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

package talos

import "strings"

// GoldenName is the shared image every tenant of one Talos version clones.
//
// Deterministic, and deliberately the catalogue key with its dots flattened for
// DNS-1123. Catalogue and image naming join at one identifier rather than
// agreeing by convention — and because the name is derived, two tenants asking
// for the same version ask for the same object, which is the whole mechanism
// behind one import instead of one per tenant.
func GoldenName(version string) string {
	return "talos-golden-" + strings.ReplaceAll(
		strings.TrimPrefix(strings.TrimSpace(version), "v"), ".", "-")
}
