"""Which Talos versions this deployment offers, and which Kubernetes they take.

Curated, not discovered. The image factory serves images by version; the
compatibility matrix lives in Talos's release notes, so "read it from the
factory" means scraping documentation. A live external feed is also the wrong
dependency for a private cloud — the same reason the signer image moves to our
own registry — and "new versions without a release" is an anti-feature here: a
Talos version must appear in the wizard only after its golden image has been
imported and smoked. A catalogue that grew a version nobody verified would
break the product's own discipline.

One module owns it, and one `is_compatible()` answers the question. Both
readers go through it — the endpoint the wizard renders and the validator that
accepts a create request — because the failure mode when they diverge is the
one this codebase keeps meeting: the wizard offers a pair the backend then
refuses, or worse, accepts.

The record key is the Talos version, and it is deliberately the same
identifier that will name the shared golden image (T2): catalogue and image
naming join at one string rather than agreeing by convention.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

CATALOG_ENV = "TENANTS_TALOS_CATALOG"

# The empty schematic — no system extensions; a KubeVirt worker needs none.
# `openstack` and not `nocloud`: CAPK attaches a cloudInitConfigDrive, which is
# an OpenStack config-2 disk. The nocloud variant looks for a `cidata` disk,
# does not find one, and sits in maintenance mode waiting for
# `talosctl apply-config` — a worker that boots fine and never joins.
_EMPTY_SCHEMATIC = (
    "376567988ad370138ad8b2698212367b8edcb69b5fd68c80be1f2ec7d603b4ba"
)


def factory_image_url(talos_version: str, schematic: str = _EMPTY_SCHEMATIC) -> str:
    return (
        f"https://factory.talos.dev/image/{schematic}/"
        f"v{talos_version.lstrip('v')}/openstack-amd64.raw.xz"
    )


@dataclass(frozen=True)
class TalosRelease:
    """One offered Talos version and the Kubernetes window it supports."""

    talos: str
    image_url: str
    k8s_min: str
    k8s_max: str
    sha: str = ""
    default: bool = False


# What the lab runs today, and what the hardcoded URL pointed at. Talos 1.13
# supports Kubernetes 1.31 through 1.36.
BUILT_IN_CATALOG: tuple[TalosRelease, ...] = (
    TalosRelease(
        talos="1.13.8",
        image_url=factory_image_url("1.13.8"),
        k8s_min="1.31",
        k8s_max="1.36",
        default=True,
    ),
)


def _minor(version: str) -> tuple[int, int] | None:
    """(major, minor) from `v1.32.1`, `1.32.1` or `1.32`.

    Compared as numbers, never as strings: "1.9" sorts above "1.31" as text,
    which would silently accept a version outside the window.
    """
    m = re.match(r"^v?(\d+)\.(\d+)", (version or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def is_compatible(release: TalosRelease, k8s_version: str) -> bool:
    """The single fact. Both readers ask this and nothing else.

    Compared at minor-version granularity: the window is stated as 1.31–1.36
    and a tenant asks for 1.32.1, so patch releases must not narrow it.
    """
    want = _minor(k8s_version)
    lo, hi = _minor(release.k8s_min), _minor(release.k8s_max)
    if want is None or lo is None or hi is None:
        return False
    return lo <= want <= hi


def _parse(raw: str) -> list[TalosRelease]:
    entries: list[TalosRelease] = []
    for item in json.loads(raw):
        try:
            entries.append(TalosRelease(
                talos=str(item["talos"]),
                image_url=str(item.get("image_url") or factory_image_url(item["talos"])),
                k8s_min=str(item["k8s_min"]),
                k8s_max=str(item["k8s_max"]),
                sha=str(item.get("sha", "")),
                default=bool(item.get("default", False)),
            ))
        except (KeyError, TypeError) as e:
            raise ValueError(f"catalogue entry {item!r} is missing {e}") from e
    if not entries:
        raise ValueError("catalogue is empty")
    return entries


def catalog() -> list[TalosRelease]:
    """The offered releases.

    A malformed override falls back to the built-in rather than leaving the
    deployment with no Talos at all — and says so loudly, because the operator
    who wrote it will otherwise see their new version simply not appear.
    """
    raw = os.getenv(CATALOG_ENV)
    if not raw:
        return list(BUILT_IN_CATALOG)
    try:
        return _parse(raw)
    except (ValueError, json.JSONDecodeError) as e:
        logger.error(
            "%s is not a usable catalogue (%s) — falling back to the built-in "
            "list; the versions configured there are NOT offered.",
            CATALOG_ENV, e,
        )
        return list(BUILT_IN_CATALOG)


def default_release() -> TalosRelease:
    """The one the wizard preselects.

    First entry marked default; failing that, the first entry — a catalogue
    with no default is a configuration slip, not a reason to refuse to create
    anything.
    """
    entries = catalog()
    return next((e for e in entries if e.default), entries[0])


def releases_for(k8s_version: str) -> list[TalosRelease]:
    return [e for e in catalog() if is_compatible(e, k8s_version)]


def find(talos_version: str) -> TalosRelease | None:
    wanted = (talos_version or "").lstrip("v")
    return next((e for e in catalog() if e.talos == wanted), None)


def compatible_pairs() -> set[tuple[str, str]]:
    """Every (talos, k8s-minor) this deployment will accept.

    Used by the anti-drift test to compare what the endpoint publishes against
    what the validator lets through — the sets have to be equal, and testing
    that is testing the meaning rather than copying the implementation.
    """
    pairs: set[tuple[str, str]] = set()
    for e in catalog():
        lo, hi = _minor(e.k8s_min), _minor(e.k8s_max)
        if lo is None or hi is None:
            continue
        for minor in range(lo[1], hi[1] + 1):
            pairs.add((e.talos, f"{lo[0]}.{minor}"))
    return pairs
