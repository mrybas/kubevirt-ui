#!/usr/bin/env bash
# What to tag the images and the chart with, decided from the git ref.
#
# A shell function inside a workflow is code nobody can run, and this one holds
# a rule with teeth: a CalVer tag is a release and also moves `latest`, while
# anything else must never be pullable as `latest`, because an image that can be
# pulled as `latest` is eventually deployed as one.
#
# A **prerelease** — a tag with a hyphen, `2026.10.37-dev.1` — is the third case
# and it did not exist before. It publishes real artefacts under its own version,
# including the chart, and it must not move `latest` either: the point of a dev
# release is to be installable on purpose and unreachable by accident.
#
#   hack/image-tags.sh <ref> [registry] [repository]
set -euo pipefail

ref="${1:?git ref, e.g. refs/tags/2026.10.37-dev.1 or refs/heads/operator-dev}"
registry="${2:-ghcr.io}"
repository="${3:-mrybas/kubevirt-ui}"
sha="${GITHUB_SHA:-0000000}"

versions=()
case "$ref" in
refs/tags/*)
	version="${ref#refs/tags/}"
	is_release="true"
	versions+=("$version")
	case "$version" in
	*-*)
		# A prerelease. SemVer resolvers exclude these from ranges unless asked
		# for by name, which is the isolation being bought here — and Helm and
		# Flux both honour that, so a chart published this way cannot be
		# selected by a `2026.10.*` that means production.
		is_prerelease="true"
		;;
	*)
		is_prerelease="false"
		versions+=("latest")
		;;
	esac
	;;
*)
	is_release="false"
	is_prerelease="false"
	version="dev-${sha:0:7}"
	versions+=("$version")
	;;
esac

for component in backend frontend operator; do
	list=""
	for tag in "${versions[@]}"; do
		list="${list:+${list},}${registry}/${repository}/${component}:${tag}"
	done
	echo "tags_${component}=${list}"
done
echo "version=${version}"
echo "is_release=${is_release}"
echo "is_prerelease=${is_prerelease}"
