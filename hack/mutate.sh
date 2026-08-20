#!/usr/bin/env bash
# Run a mutation test without touching the tree anybody else is reading.
#
# A mutation applied in place is visible to whoever looks at the repo while it
# runs, and a deliberately broken file reads exactly like a defect — it has been
# reported as one three times. This copies the working tree, including anything
# uncommitted, into a throwaway directory and mutates there.
#
#   hack/mutate.sh <file-relative-to-repo> <sed-expression> <go-test-args...>
#
# Example:
#   hack/mutate.sh operator/internal/controller/managedtenant_transit.go \
#     's/if len(unprotected) == 0 && !hasDeny/if !hasDeny/' \
#     -run TestTheBaselineIsWithheld
#
# It prints whether the mutant was actually applied — a mutation run that
# silently changed nothing is worse than none, because it reads as "the test
# caught nothing to catch".
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target="${1:?file to mutate}"; shift
expression="${1:?sed expression}"; shift

work="$(mktemp -d "${TMPDIR:-/tmp}/mutate.XXXXXX")"
trap 'rm -rf "$work"' EXIT

# The working tree, not HEAD: the change under test is usually uncommitted, and
# that is the caveat that made the first worktree attempt measure the old code.
# Only what the Go suite needs — copying the frontend's dependencies costs a
# minute and leaves a directory nothing can delete.
tar -C "$repo" --exclude .git --exclude .mutation -cf - operator test \
	| tar -C "$work" -xf -

before="$(md5 -q "$work/$target" 2>/dev/null || md5sum "$work/$target" | cut -d' ' -f1)"
sed -i.orig "$expression" "$work/$target"
after="$(md5 -q "$work/$target" 2>/dev/null || md5sum "$work/$target" | cut -d' ' -f1)"
if [ "$before" = "$after" ]; then
	echo "мутація нічого не змінила — вираз не збігся; тест нічого не доводить" >&2
	exit 2
fi
echo "мутація застосована у $work"

docker run --rm -v "$work:/work" -w /work/operator \
	-e KUBEBUILDER_ASSETS=/work/operator/bin/k8s/1.36.0-linux-arm64 \
	--entrypoint go "$(docker inspect -f '{{.Config.Image}}' kvbuild)" \
	test ./internal/... -count=1 "$@" 2>&1 | grep -E "^(---|ok|FAIL|\s+\S+_test\.go:)" | head -20
