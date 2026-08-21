# Releasing

Three kinds of push, and they must not be confused. The rule itself is
`hack/image-tags.sh`, which is runnable and tested — the table below is what it
does, not a second copy of it.

| push | images | chart | `latest` |
|---|---|---|---|
| `2026.10.37` | that version | published | **moves** |
| `2026.10.37-dev.1` | that version | published | untouched |
| branch `operator-dev` | `dev-<sha7>` | not published | untouched |

## Why a prerelease exists

Until it did, the only way to publish a chart — and therefore the only way to
install the operator anywhere from the chart — was a CalVer tag, and a CalVer tag
is the line that means production.

A prerelease publishes everything under its own version and is invisible to
anything that did not ask for it by name: SemVer resolvers exclude prereleases
from ranges, so a `2026.10.*` that means production will not select
`2026.10.37-dev.1`. Helm and Flux both honour that. What no resolver protects is
`latest`, so a prerelease does not move it.

Promotion is dropping the suffix: `2026.10.37-dev.1` → `2026.10.37`, same chart
name, same registry, same images rebuilt from the same tree.

## Cutting one

```bash
git tag 2026.10.37-dev.1 && git push origin 2026.10.37-dev.1
```

CI rewrites `version` and `appVersion` in `Chart.yaml` from the tag, so the
chart's three images (`backend`, `frontend`, `operator`) all resolve to it —
they default to `.Chart.AppVersion`.

## Installing one

The chart needs the site's own facts and refuses to render without them, by
name and with the reason. There are no defaults for these: each has been wrong
once, and each time the failure was silent.

```bash
helm install kubevirt-ui \
  oci://ghcr.io/mrybas/kubevirt-ui/charts/kubevirt-ui \
  --version 2026.10.37-dev.1 \
  --namespace kubevirt-ui-system --create-namespace \
  --set operator.enabled=true \
  --set operator.config.kubeOvnNamespace=o0-kube-ovn \
  --set operator.config.metallbNamespace=o0-metallb \
  --set operator.config.metallbPool=cp-transit-pool \
  --set operator.config.cpTransitSubnet=cp-transit \
  --set operator.config.ingressDomain=tenants.lab.beardlabs.cc
```

On a **fresh** cluster that is the whole of it. On a cluster that ran the
product before the chart carried the operator, Helm will refuse to manage
objects it did not create — nine CRDs applied by kustomize, and the three
`kubevirt-ui-tenant-*` ClusterRoles applied by hand from chart 0.1.0, which
carry the Helm labels but not the ownership annotations.

```bash
hack/adopt-into-helm.sh kubevirt-ui kubevirt-ui-system          # says what it would do
hack/adopt-into-helm.sh kubevirt-ui kubevirt-ui-system --apply
```

It writes ownership metadata and nothing else. Deleting the CRDs — the other way
to make Helm happy — cascade-deletes every tenant, network and VM described by
them.

## Handover flags

The operator takes work over from the product one path at a time, and the flags
are the cutover. **They follow `operator.enabled`**, so an install that asks for
the operator gets the four handed-over paths handed over:

```
OPERATOR_UNDERLAY_ENABLED         true
OPERATOR_TENANT_BOOTSTRAP_ENABLED true
OPERATOR_TENANT_TIME_ENABLED      true
OPERATOR_TENANT_ADDONS_ENABLED    true
```

They used to default to off, which produced the worst kind of install: three
healthy controllers and a product that keeps writing everything, with nothing
broken, nothing logged, and no symptom except that none of it does anything.

The rest — image, vm, template, announce, network — stay off. They name paths
the product still owns.

Any of them can be decided explicitly, and a decision wins:

```yaml
backend:
  env:
    OPERATOR_TENANT_TIME_ENABLED: "false"
```

Setting a flag on an image that predates it is a cutover that looks done and is
not — that happened here, and the backend had to be rolled first.
