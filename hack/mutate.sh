#!/usr/bin/env bash
# Run a mutation test without touching the tree anybody else is reading.
#
# A mutation applied in place is visible to whoever looks at the repo while it
# runs, and a deliberately broken file reads exactly like a defect — it has been
# reported as one three times. This copies the working tree, including anything
# uncommitted, into a throwaway directory and mutates there.
#
#   hack/mutate.sh <file-relative-to-repo> <sed-expression> <test-args...>
#
# A path under `backend/` runs pytest instead of the Go suite, and the test
# arguments are pytest's. That case is not a convenience: the compose service
# bind-mounts `backend/` into the container, so a mutation applied "inside the
# container" is a mutation of the working tree — which is exactly how the
# in-tree mutation happened again after the first two were fixed.
#
# Examples:
#   hack/mutate.sh operator/internal/controller/managedtenant_transit.go \
#     's/if len(unprotected) == 0 && !hasDeny/if !hasDeny/' \
#     -run TestTheBaselineIsWithheld
#
#   hack/mutate.sh frontend/src/components/vm/CreateVMWizard.tsx \\
#     "s/reason: error instanceof Error ? error.message : String(error)/reason: ''/" \\
#     src/components/vm/__tests__/BatchFailureReason.test.tsx
#
#   hack/mutate.sh backend/app/api/v1/tenants_crud.py \
#     's/if resource_version:/if False and resource_version:/' \
#     tests/test_addons_have_one_writer.py
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
case "$target" in
backend/*)         trees="backend" ;;
frontend/*)        trees="frontend" ;;
hack/*|helm/*)     trees="operator test hack helm" ;;
*)                 trees="operator test" ;;
esac
# shellcheck disable=SC2086
# node_modules is excluded and supplied from the image instead: copying it costs
# a minute and leaves a directory that is awkward to delete.
tar -C "$repo" --exclude .git --exclude .mutation --exclude __pycache__ \
	--exclude node_modules -cf - $trees | tar -C "$work" -xf -

before="$(md5 -q "$work/$target" 2>/dev/null || md5sum "$work/$target" | cut -d' ' -f1)"
sed -i.orig "$expression" "$work/$target"
# BSD sed needs a backup suffix, and the backup must not survive: dropped into
# helm/kubevirt-ui/templates/ it is a second template, and Helm renders it. The
# mutant then runs beside an unmutated copy of itself — measured, and it turned
# a real kill into a confusing one: the assertions read the second copy's values
# and passed, and only a count noticed anything was wrong.
rm -f "$work/$target.orig"
after="$(md5 -q "$work/$target" 2>/dev/null || md5sum "$work/$target" | cut -d' ' -f1)"
if [ "$before" = "$after" ]; then
	echo "мутація нічого не змінила — вираз не збігся; тест нічого не доводить" >&2
	exit 2
fi
echo "мутація застосована у $work"

if [ "$trees" = "frontend" ]; then
	# The compose service bind-mounts frontend/ too, so a `sed -i` "in the
	# container" is a `sed -i` in the working tree — the same door the backend
	# case was added for, and a hand-rolled cp/restore around it is the habit
	# wearing gloves. The anonymous volume seeds node_modules from the image,
	# which is what compose does.
	docker run --rm -v "$work/frontend:/app" -v /app/node_modules -w /app \
		kubevirt-ui-frontend npx vitest run "$@" 2>&1 | tail -12
	exit 0
fi

if [ "$trees" = "backend" ]; then
	# The same image the compose service uses, but mounted read-write at the
	# copy — never at the repo.
	image="$(docker compose -f "$repo/docker-compose.yml" config --format json 2>/dev/null \
		| python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["backend"]["image"])' \
		2>/dev/null || echo kubevirt-ui-backend)"
	docker run --rm -v "$work/backend:/app" -w /app "$image" \
		pytest -q "$@" 2>&1 | tail -12
	exit 0
fi

docker run --rm -v "$work:/work" -w /work/operator \
	-e KUBEBUILDER_ASSETS=/work/operator/bin/k8s/1.36.0-linux-arm64 \
	--entrypoint go "$("$(dirname "${BASH_SOURCE[0]}")/gotest-image.sh")" \
	test ./internal/... -count=1 -timeout 300s "$@" 2>&1 \
	| grep -E "^(---|ok|FAIL|panic|\s+\S+_test\.go:)" | head -20
