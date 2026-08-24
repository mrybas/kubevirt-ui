#!/usr/bin/env bash
# Run the suite N times against a snapshot of the tree, keeping every run's
# output, and stop at the first one that is not green.
#
# Two reasons it copies rather than running in place. A run takes about five
# minutes, so a series of them outlives any foreground call and has to be left
# in the background — and anything left running in the background would
# otherwise measure a tree that is still being edited, which has already
# invalidated one series here.
#
#   hack/soak.sh <runs> [go-test-args...]
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runs="${1:?how many runs}"; shift || true

work="$(mktemp -d "${TMPDIR:-/tmp}/soak.XXXXXX")"
out="$work/results"; mkdir -p "$out"
tar -C "$repo" --exclude .git --exclude .mutation -cf - operator test | tar -C "$work" -xf -
echo "знімок: $work"

image="$("$(dirname "${BASH_SOURCE[0]}")/gotest-image.sh")"
for i in $(seq 1 "$runs"); do
	docker run --rm -v "$work:/work" -w /work/operator \
		-e KUBEBUILDER_ASSETS=/work/operator/bin/k8s/1.36.0-linux-arm64 \
		--entrypoint go "$image" \
		test ./internal/controller/ -count=1 -timeout 900s "$@" \
		> "$out/run$i.txt" 2>&1 || true
	if grep -qE '^--- FAIL' "$out/run$i.txt"; then
		echo "прогін $i ЧЕРВОНИЙ — $out/run$i.txt"
		grep -E '^--- FAIL' -A 2 "$out/run$i.txt" | head -12
		exit 1
	fi
	echo "прогін $i: $(tail -1 "$out/run$i.txt")"
done
echo "серію завершено: $runs зелених, вивід у $out"
