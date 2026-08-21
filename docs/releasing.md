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
  --set operator.config.ingressDomain=tenants.lab.beardlabs.cc \
  --set operator.config.tenantSupernet=10.200.0.0/14
```

The supernet is the same fact the backend reads as `TENANT_SUPERNET`. Set it in
one place; the chart refuses to render if both are set and disagree.

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

## Admission

The operator validates `ManagedTenant` and `ManagedVM` before they are stored,
and the product's create endpoint passes the refusal through as a 400 — that
message is the only sentence saying which field is wrong. It is off by default
and needs cert-manager:

```
--set operator.webhooks.enabled=true
```

Two of the three webhooks fail closed, so between the configuration existing and
the certificate being issued nothing can be created. That is the trade: with it
off, a description the operator cannot build is accepted and the reason appears
later as a condition instead of in the answer.

It is served by the vm deployment. Asking for it with that domain disabled is
refused at render, because a configuration pointing at a service with no
endpoints rejects every write it guards.

## Handover flags

The operator takes work over from the product one path at a time, and the flags
are the cutover. **They follow `operator.enabled`**, so an install that asks for
the operator gets the four handed-over paths handed over:

```
OPERATOR_UNDERLAY_ENABLED         true
OPERATOR_TENANT_ENABLED           true
OPERATOR_TENANT_BOOTSTRAP_ENABLED true
OPERATOR_TENANT_TIME_ENABLED      true
OPERATOR_TENANT_ADDONS_ENABLED    true
```

The tenant flags move together. Three of them write parts of a tenant, and
handing those over without the tenant itself gives them to a controller that has
never heard of it — the backend stops writing the addons because the flag says
they are not its job, and nothing else writes them. A tenant built that way
comes up with no CNI and never recovers.

A deployment that wants the parts without the whole — every tenant adopted by
hand — says so:

```yaml
backend:
  env:
    OPERATOR_TENANT_ENABLED: "false"
```

**Setting a flag an image predates is not a handover.** The image must contain
the path the flag names; `OPERATOR_TENANT_ENABLED` on a backend that has never
heard of it leaves the endpoint building tenants itself while the parts are
handed away, which is the same broken shape from the other direction.

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
