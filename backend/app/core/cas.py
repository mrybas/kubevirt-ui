"""Compare-and-set writes for list fields inside a shared CR spec.

Several kube-ovn specs keep a *list* where more than one of our flows appends:
`Vpc.spec.vpcPeerings`, `Vpc.spec.staticRoutes`, `Vpc.spec.policyRoutes`,
`Subnet.spec.acls`. A merge patch (RFC 7386) replaces an array wholesale, so
the read-modify-write

    item = get(...)                    # list has [A]
    lst   = item.spec.field + [B]      # local copy is [A, B]
    patch({"spec": {"field": lst}})    # writes [A, B]

is only correct while nobody else writes between the read and the patch. When
two flows do it at once both compute from the same read and the second patch
drops the first entry — both calls report success and half the configuration
quietly does not exist. This was seen live with concurrent VPC peerings
(four links created, four of the eight sides missing).

Sending `metadata.resourceVersion` in the body makes the API server reject a
patch computed from a stale read with 409, and the retry recomputes against
the winner.

Note for operators reading this from the outside: the same trap applies to
`kubectl patch --type merge -p '{"spec":{"vpcPeerings":[...]}}'`. That command
does not add an entry, it *replaces the list*. Use `--type json` with
`{"op": "add", "path": "/spec/vpcPeerings/-"}` instead.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from kubernetes_asyncio.client.exceptions import ApiException

from app.core.constants import KUBEOVN_API_GROUP, KUBEOVN_API_VERSION

logger = logging.getLogger(__name__)

DEFAULT_RETRIES = 5


async def patch_spec_with_retry(
    k8s,
    plural: str,
    name: str,
    mutate: Callable[[dict[str, Any]], dict[str, Any] | None],
    *,
    group: str = KUBEOVN_API_GROUP,
    version: str = KUBEOVN_API_VERSION,
    retries: int = DEFAULT_RETRIES,
    namespace: str | None = None,
) -> bool:
    """Re-read `name`, build a spec patch from `mutate`, write it under CAS.

    `mutate` receives the *current* spec and returns the fragment to merge into
    it — or None when there is nothing to do, in which case no request is sent
    and the call returns False. It may be invoked more than once, so it must be
    free of side effects.

    Returns True when a patch was written. A 404 on the object is not an error:
    nothing to patch means nothing to do.
    """
    get_kwargs: dict[str, Any] = {"group": group, "version": version, "plural": plural, "name": name}
    if namespace is not None:
        get_kwargs["namespace"] = namespace

    for attempt in range(retries):
        try:
            item = await _get(k8s, namespace, get_kwargs)
        except ApiException as e:
            if e.status == 404:
                return False
            raise

        patch = mutate(item.get("spec", {}) or {})
        if patch is None:
            return False

        body = {
            "metadata": {"resourceVersion": item["metadata"]["resourceVersion"]},
            "spec": patch,
        }
        try:
            await _patch(k8s, namespace, {**get_kwargs, "body": body})
            return True
        except ApiException as e:
            if e.status == 409 and attempt < retries - 1:
                logger.info(
                    "CAS patch on %s/%s raced another writer (attempt %d); recomputing",
                    plural, name, attempt + 1,
                )
                await asyncio.sleep(0.1 * (2 ** attempt))
                continue
            raise

    return False


async def _get(k8s, namespace: str | None, kwargs: dict[str, Any]) -> dict[str, Any]:
    if namespace is None:
        return await k8s.custom_api.get_cluster_custom_object(**kwargs)
    return await k8s.custom_api.get_namespaced_custom_object(**kwargs)


async def _patch(k8s, namespace: str | None, kwargs: dict[str, Any]) -> Any:
    call: Callable[..., Awaitable[Any]] = (
        k8s.custom_api.patch_cluster_custom_object if namespace is None
        else k8s.custom_api.patch_namespaced_custom_object
    )
    return await call(**kwargs, _content_type="application/merge-patch+json")


def upsert(items: list[dict[str, Any]], entry: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Replace the element whose `key` matches `entry`'s, else append.

    Everything that does not match is preserved — that is the whole point.
    """
    out = [i for i in items if i.get(key) != entry.get(key)]
    out.append(entry)
    return out
