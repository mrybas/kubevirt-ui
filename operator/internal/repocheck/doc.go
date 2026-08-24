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

// Package repocheck holds guards over things in this repository that are not
// Go: shell scripts, workflow rules, generated manifests.
//
// It exists because of a mistake made twice in one day. The natural home for
// such a check looks like the backend's pytest suite, and a check written there
// passes — vacuously. That suite runs in a container which mounts only
// `backend/`, so anything reaching for `hack/` or `helm/` finds nothing and
// skips, and a skip in a green run is indistinguishable from a check.
//
// The Go suite runs with the whole repository mounted, so guards that need to
// see the repository live here. Nothing in this package ships in the operator.
package repocheck
