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

# The long-lived interactive container, if it exists, must come from this same
# image. It used to be created by hand, and a tool installed into it by hand
# made a test pass there and fail in every throwaway container — the same class
# as a check that only runs where it can see its input, pointed the other way.
if docker container inspect kvbuild >/dev/null 2>&1; then
	current="$(docker container inspect -f '{{.Config.Image}}' kvbuild)"
	if [ "$current" != "$image" ]; then
		echo "kvbuild працює з $current, а не з $image — перестворити:" >&2
		echo "  docker rm -f kvbuild && docker run -d --name kvbuild \\" >&2
		echo "    -v \"\$PWD:/work\" -w /work/operator $image sleep infinity" >&2
	fi
fi
