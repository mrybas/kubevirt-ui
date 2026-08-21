#!/usr/bin/env bash
# The image the Go suite runs in, built if it is not there yet.
#
# Prints the name and nothing else, so callers can use it directly.
set -euo pipefail
image="kubevirt-ui-gotest:local"
if ! docker image inspect "$image" >/dev/null 2>&1; then
	echo "будую ${image} …" >&2
	docker build -q -f "$(dirname "${BASH_SOURCE[0]}")/Dockerfile.gotest" \
		-t "$image" "$(dirname "${BASH_SOURCE[0]}")" >&2
fi
echo "$image"
