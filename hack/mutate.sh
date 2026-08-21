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
backend/*) trees="backend" ;;
hack/*)    trees="operator test hack helm" ;;
*)         trees="operator test" ;;
esac
# shellcheck disable=SC2086
tar -C "$repo" --exclude .git --exclude .mutation --exclude __pycache__ \
	-cf - $trees | tar -C "$work" -xf -

before="$(md5 -q "$work/$target" 2>/dev/null || md5sum "$work/$target" | cut -d' ' -f1)"
sed -i.orig "$expression" "$work/$target"
after="$(md5 -q "$work/$target" 2>/dev/null || md5sum "$work/$target" | cut -d' ' -f1)"
if [ "$before" = "$after" ]; then
	echo "мутація нічого не змінила — вираз не збігся; тест нічого не доводить" >&2
	exit 2
fi
echo "мутація застосована у $work"

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
	--entrypoint go "$(docker inspect -f '{{.Config.Image}}' kvbuild)" \
	test ./internal/... -count=1 -timeout 300s "$@" 2>&1 \
	| grep -E "^(---|ok|FAIL|panic|\s+\S+_test\.go:)" | head -20
