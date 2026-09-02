"""Planning a snapshot-then-publish.

The VM keeps running: we snapshot its disk, make a temporary PVC from the
snapshot, and let a Job read that. Cross-namespace and same-namespace clones
are both thin on snapshot-capable storage, so the copy costs little.

The plan is data, not actions, so the ordering and the naming can be tested
without a cluster.
"""

import math
import uuid
from typing import Any

from app.core.harbor_client import HarborNotFound

PUBLISH_IMAGE = "gcr.io/go-containerregistry/crane:debug"

# The image's shell lives at `/busybox/sh`, not `/bin/sh` — a hardcoded
# `/bin/sh` here left every publish Job unable to start at all (kubelet
# CreateContainerError: exec: "/bin/sh": no such file or directory), never
# caught by this suite because a mocked Kubernetes API never execs anything.
# Bare `sh` resolves through PATH (busybox symlinks it there) and keeps working
# if the base image is ever swapped for one with a conventional /bin/sh layout.
# One constant, so the decision can never drift between two call sites.
_SHELL = "sh"

# THE USERLAND IS BUSYBOX. This one fact has now caused three separate
# defects in this file, each of which passed the whole test suite and died on
# the Job's first line in a real cluster:
#
#   1. `command: ["/bin/sh", "-c"]` — there is no /bin/sh; the shell is at
#      /busybox/sh and on PATH as bare `sh` (fixed in f5cc38f).
#   2. `dd ... conv=sparse` — BusyBox `dd` accepts only
#      notrunc/sync/noerror/fsync/swab for `conv`, and answers anything else
#      with "dd: invalid argument 'sparse' to 'conv'", exit 1. Under
#      `set -eu` that aborted every Block publish before `crane auth login`.
#   3. (the same class) any GNU-only flag reached for from a man page.
#
# Nothing in this suite executes these scripts — a mocked Kubernetes API never
# execs anything — so the only defences are this comment and the operand test
# in test_a_block_mode_disk_gets_a_scratch_copy_before_it_can_be_tarred.py.
# Before adding a flag here, check it against BusyBox's applet, not GNU
# coreutils:
#     docker run --rm --entrypoint sh gcr.io/go-containerregistry/crane:debug \
#         -c '<the exact command>; echo EXIT=$?'

# Where the raw block device is attached when the source disk is Block-mode.
# volumeDevices (not volumeMounts) is the only way Kubernetes exposes a Block
# PVC to a container — there is no filesystem underneath it to mount.
_BLOCK_DEVICE_PATH = "/dev/publish-disk"

# The lab's existing publish pattern: pack the disk into a single-layer OCI
# image and append it to an empty base — the shape both CDI's
# `source.registry` and KubeVirt's `containerDisk` can consume, a directory
# called `disk/` holding the image file, whatever it is named inside.
#
# Filesystem source: the temporary PVC mounts as a filesystem at `/work/disk`
# (not `/disk`), so `disk` is already that directory and `tar` run from
# `/work` needs nothing else.
#
# The archive is streamed into crane rather than written to `layer.tar`
# first, for the same reason the Block branch does it — except here the
# reason is worse. `/work` is the container's own writable layer, which is
# node EPHEMERAL storage: a full-size tar of a 100GB disk written there fills
# the node's filesystem and evicts unrelated pods through disk pressure.
# Refusing an emptyDir for the Block scratch (see below) and then writing the
# same volume of data to an even less suitable place on the Filesystem branch
# would have been the same mistake with a different name. `crane append -f -`
# reads the tarball from stdin — verified by execution against the real
# crane:debug image, not only from its source — so nothing is materialised at
# all here: the temporary PVC is read and the bytes go straight out.
#
# `pipefail` matters for the same reason it does below: the pipeline's
# default exit status under plain `set -e` is crane's, not tar's, so a tar
# that died mid-stream would otherwise push a truncated image that looks
# valid and boots to garbage.
_FILESYSTEM_PUSH_SCRIPT = (
    "set -eu -o pipefail; "
    "cd /work && "
    'crane auth login "$REGISTRY" -u "$ROBOT_USER" -p "$ROBOT_PASS" && '
    'tar -cf - disk | crane append --oci-empty-base -f - -t "$REF"'
)

# Block source: there is no filesystem to mount, so the temporary PVC is
# attached as a raw block device instead (`volumeDevices`), and `dd` copies it
# into a regular file — in full, including the empty parts. `conv=sparse`
# would skip those, and BusyBox `dd` does not implement it (see the userland
# note above); the scratch PVC is sized by `scratch_pvc_size()` for a full
# copy, so a dense copy is correct, only slower on a thin disk — a block device cannot be tarred directly, and crane
# needs a regular file — on a scratch PVC (never emptyDir: a disk this size
# on node ephemeral storage risks evicting unrelated pods through disk
# pressure).
#
# The archive is piped straight into crane rather than materialised as a
# second file: `tar -cf layer.tar disk` followed by `crane append -f
# layer.tar` would need `layer.tar` and `disk.img` alive on the scratch PVC
# at the same time (tar has to finish reading disk.img before anything could
# delete it), so peak usage would be ~2x the source capacity against a PVC
# sized for one copy — an ENOSPC partway through `tar`, not a clean failure.
# `crane append -f -` reads the tarball from stdin: confirmed against
# crane's own source (`pkg/crane/append.go`, `streamFile`), which special-
# cases `path == "-"` to `os.Stdin` and wraps it in a genuine streaming
# layer (`stream.NewLayer`) rather than falling back to a seekable-file
# read — this is a real, intentional feature, not an accident of the flag's
# name. That keeps peak scratch usage to the one `dd` copy, matching what
# the scratch PVC is actually sized for.
#
# `pipefail` matters here specifically because the pipeline's default exit
# status (under plain `set -e`) is `crane`'s, not `tar`'s — a `tar` that
# died output-starved would otherwise not fail the script at all. If this
# image's shell does not support `-o pipefail` the `set` line itself
# fails immediately, which is a clear failure at the very first line rather
# than a silent partial push.
_BLOCK_PUSH_SCRIPT = (
    "set -eu -o pipefail; "
    "mkdir -p /scratch/disk && "
    f'dd if="{_BLOCK_DEVICE_PATH}" of=/scratch/disk/disk.img bs=4M && '
    'crane auth login "$REGISTRY" -u "$ROBOT_USER" -p "$ROBOT_PASS" && '
    "cd /scratch && "
    'tar -cf - disk | crane append --oci-empty-base -f - -t "$REF"'
)


# A Job's name is used as a label value (`job-name`), so it is capped at 63
# characters — while the PVC it is derived from is a DNS-1123 subdomain and
# may be up to 253. A name built by concatenation alone therefore fails to
# create at all for a long-named disk, and fails identically (AlreadyExists)
# for a disk published twice inside `ttlSecondsAfterFinished`, which keeps a
# successful Job around for an hour.
_JOB_NAME_MAX = 63
_JOB_PREFIX = "publish-"
_JOB_SUFFIX_LEN = 8


def publish_job_name(pvc: str) -> str:
    """A Job name that is unique per publish and always within the 63-char cap.

    Unique, because `ttlSecondsAfterFinished: 3600` means the Job from a
    publish ten minutes ago is still there: a name derived from the disk
    alone makes the second publish of the same disk an AlreadyExists — which
    is not a conflict the user caused in any way they could act on.

    Bounded, because a 200-character PVC name would otherwise produce a Job
    name the API server refuses outright. The disk's name is truncated, never
    the random suffix: two disks whose names agree in the first 46 characters
    are still told apart by the suffix, whereas truncating the suffix would
    reintroduce the collision this exists to remove.
    """
    suffix = uuid.uuid4().hex[:_JOB_SUFFIX_LEN]
    budget = _JOB_NAME_MAX - len(_JOB_PREFIX) - 1 - _JOB_SUFFIX_LEN
    stem = pvc[:budget].rstrip("-.")
    return f"{_JOB_PREFIX}{stem}-{suffix}" if stem else f"{_JOB_PREFIX}{suffix}"


# ext4 (and xfs) spend part of a fresh filesystem on metadata — inode tables,
# journal, and the 5% root-reserved blocks — so a PVC of exactly N bytes
# holds noticeably less than N bytes of file. Measured range is ~0.93-0.95
# usable, and `dd` writes the full source capacity into a file on it, so a
# scratch sized 1:1 with the source ENOSPCs partway through a full disk.
_SCRATCH_MARGIN = 1.15
# Plus a flat allowance, because the percentage alone is thin for a small
# disk (a 1Gi source gains only 150Mi, and a fresh ext4's own overhead on a
# volume that size is a bigger share than on a large one).
_SCRATCH_FLOOR_BYTES = 512 * 1024**2

_QUANTITY_UNITS = {
    "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5,
    "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4, "P": 1000**5,
}


def _parse_quantity(value: str) -> int:
    """Bytes for a Kubernetes quantity string, or 0 if it cannot be read."""
    text = (value or "").strip()
    for unit, mult in _QUANTITY_UNITS.items():
        if text.endswith(unit):
            try:
                return int(float(text[: -len(unit)]) * mult)
            except ValueError:
                return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def scratch_pvc_size(source_size: str) -> str:
    """How big the scratch PVC must be to hold a full copy of `source_size`.

    Deliberately larger than the source: `dd` writes exactly the source
    capacity into a FILE, and a filesystem cannot store its own size in
    files. Rounded up to whole Gi both because CSI provisioners round up
    anyway and because a scratch volume is short-lived — over-asking costs an
    hour of capacity, under-asking costs the whole publish, at the end, after
    the copy has already run.

    An unparseable size falls back to the source string unchanged rather than
    inventing a number: that keeps today's behaviour for anything this parser
    does not understand instead of silently asking for something wrong.
    """
    raw = _parse_quantity(source_size)
    if raw <= 0:
        return source_size
    wanted = int(raw * _SCRATCH_MARGIN) + _SCRATCH_FLOOR_BYTES
    return f"{math.ceil(wanted / 1024**3)}Gi"


# A publish reads the whole disk twice — once through `dd` (Block) or `tar`
# (Filesystem), once over the network into the registry — so the time it needs
# is a function of the disk's size and nothing else. A fixed 1800s was fine for
# the 10Gi test image it was written against and guaranteed failure for
# anything real: a 200Gi disk cannot be copied and pushed in half an hour on
# any storage this runs on, so the Job was killed mid-push, every time,
# reporting DeadlineExceeded rather than "too slow".
#
# The floor covers everything that is not proportional to size (scheduling,
# waiting for the restored PVC to bind, the registry handshake). The rate is
# deliberately pessimistic — roughly 9 MiB/s end to end — because the cost of
# over-estimating is a Job that hangs a bit longer before failing, and the cost
# of under-estimating is a publish that never succeeds at all.
_DEADLINE_FLOOR_SECONDS = 1800
_DEADLINE_SECONDS_PER_GIB = 120
# 24h. Not a real expectation, a backstop: activeDeadlineSeconds is the only
# thing that reaps a Job that hangs, so it must stay finite however large the
# disk is.
_DEADLINE_CEILING_SECONDS = 86400


def publish_deadline_seconds(source_size: str) -> int:
    """`activeDeadlineSeconds` for publishing a disk of `source_size`.

    An unparseable size gets the floor — the same fixed value this used to
    hand every disk — rather than an invented number.
    """
    raw = _parse_quantity(source_size)
    if raw <= 0:
        return _DEADLINE_FLOOR_SECONDS
    gib = raw / 1024**3
    wanted = _DEADLINE_FLOOR_SECONDS + int(math.ceil(gib * _DEADLINE_SECONDS_PER_GIB))
    return min(wanted, _DEADLINE_CEILING_SECONDS)


def cleanup_names(job_name: str) -> tuple[str, str]:
    """Names of the two objects every publish leaves behind if it dies.

    Derived from the Job name so cleanup never has to guess, and never has to
    be told the source disk's name — deleting that would destroy the very disk
    the user asked to publish.
    """
    return f"{job_name}-snap", f"{job_name}-tmp"


def scratch_pvc_name(job_name: str) -> str:
    """Name of the Filesystem-mode scratch PVC a Block-mode publish needs.

    A Block source PVC has no filesystem to mount, so the Job `dd`s it into a
    regular file on this PVC before tarring. Named off the Job like the other
    dependents — see `cleanup_names` — so cleanup never has to be told this
    name either, and this one is created only when the source disk is Block.
    """
    return f"{job_name}-scratch"


def publish_job(
    namespace: str,
    pvc: str,
    ref: str,
    *,
    registry: str = "",
    secret_name: str = "",
    active_deadline_seconds: int | None = None,
    volume_mode: str = "Block",
    source_size: str = "",
) -> dict[str, Any]:
    """The publish Job, created SUSPENDED so it can own its dependents.

    An ownerReference needs the owner's UID, which exists only once the owner
    is created. Creating the snapshot and PVC first would leave them ownerless
    and therefore un-reaped when the Job fails.

    The container actually pushes: it packs the disk into the containerDisk
    layout and pushes it with crane, authenticating with the tenant's robot
    credential — a Secret in the target namespace holding CDI's own
    `accessKeyId`/`secretKey` shape, the same Secret CDI pulls with. Pushing
    is a registry operation, the one place a robot account works.

    `volume_mode` picks how the temporary PVC (named via `cleanup_names`) is
    attached, because that decision belongs to the container spec, not the
    PVC's own spec: `"Block"` (this codebase's convention for VM disk PVCs —
    see `images.py`'s "Required for snapshot-based cloning" DataVolume specs)
    gets `volumeDevices` plus a `dd`-then-tar script reading from
    `_BLOCK_DEVICE_PATH`, backed by a scratch PVC (see `scratch_pvc_name`) for
    the copy `dd` produces, since a Block volume has no filesystem for
    `volumeMounts` to attach to at all — Kubernetes leaves such a Pod stuck in
    `ContainerCreating`/`FailedMount` rather than refusing it up front.
    Anything else (`"Filesystem"`) keeps the original `volumeMounts` path
    unchanged.

    `registry`/`secret_name` default empty so callers that only assert on the
    Job's shape (suspend flag, naming) do not have to supply them. A real
    publish always passes both — without a registry host the push has
    nowhere to go, and without a secret it has nothing to authenticate with.

    `activeDeadlineSeconds` bounds a publish that hangs (a snapshot restore
    that never binds, a registry that never answers) so it fails loudly
    instead of sitting suspended-turned-running forever, un-reaped because it
    never reaches a terminal phase. It is derived from `source_size` (see
    `publish_deadline_seconds`) rather than fixed, because the work it bounds
    is proportional to the disk. An explicit `active_deadline_seconds` still
    wins, for tests and for an operator who knows better.
    """
    if active_deadline_seconds is None:
        active_deadline_seconds = publish_deadline_seconds(source_size)

    full_ref = f"{registry}/{ref}" if registry else ref
    env: list[dict[str, Any]] = [{"name": "REF", "value": full_ref}]
    if registry:
        env.append({"name": "REGISTRY", "value": registry})
    if secret_name:
        env.append({
            "name": "ROBOT_USER",
            "valueFrom": {
                "secretKeyRef": {"name": secret_name, "key": "accessKeyId"}
            },
        })
        env.append({
            "name": "ROBOT_PASS",
            "valueFrom": {
                "secretKeyRef": {"name": secret_name, "key": "secretKey"}
            },
        })

    job_name = publish_job_name(pvc)
    tmp_name = cleanup_names(job_name)[1]

    container: dict[str, Any] = {
        "name": "publish",
        "image": PUBLISH_IMAGE,
        "command": [_SHELL, "-c"],
        "env": env,
    }

    if volume_mode == "Block":
        container["args"] = [_BLOCK_PUSH_SCRIPT]
        container["volumeDevices"] = [
            {"name": "disk", "devicePath": _BLOCK_DEVICE_PATH}
        ]
        container["volumeMounts"] = [{"name": "scratch", "mountPath": "/scratch"}]
        volumes = [
            {
                "name": "disk",
                "persistentVolumeClaim": {"claimName": tmp_name, "readOnly": True},
            },
            {
                "name": "scratch",
                "persistentVolumeClaim": {"claimName": scratch_pvc_name(job_name)},
            },
        ]
    else:
        container["args"] = [_FILESYSTEM_PUSH_SCRIPT]
        container["volumeMounts"] = [
            {"name": "disk", "mountPath": "/work/disk", "readOnly": True}
        ]
        volumes = [
            {
                "name": "disk",
                "persistentVolumeClaim": {"claimName": tmp_name, "readOnly": True},
            },
        ]

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": job_name, "namespace": namespace},
        "spec": {
            "suspend": True,
            "backoffLimit": 1,
            "activeDeadlineSeconds": active_deadline_seconds,
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [container],
                    "volumes": volumes,
                }
            },
        },
    }


def publish_dependents(
    namespace: str,
    pvc: str,
    job_name: str,
    job_uid: str,
    *,
    storage_class: str = "",
    volume_mode: str = "Block",
    access_modes: list[str] | None = None,
    storage_size: str = "1Gi",
    snapshot_class: str = "",
) -> list[dict[str, Any]]:
    """The snapshot, the temporary PVC, and — for a Block source — the scratch
    PVC the Job's `dd` writes into. All owned by the Job.

    Ownership is what makes cleanup unconditional: Kubernetes reaps these when
    the Job goes, whether it succeeded, failed, or was deleted by hand.

    The temporary PVC copies `storageClassName`, `volumeMode` and
    `accessModes` from the disk being published — mirroring
    `disks.py`'s `rollback_snapshot` — and asks for a real size rather than a
    placeholder. Most CSI provisioners silently refuse (leave the PVC
    `Pending` forever, no `ApiException` this handler could ever see) a
    request smaller than the snapshot's `restoreSize`.

    The scratch PVC (present only when `volume_mode == "Block"`) is always
    `Filesystem` — `dd` needs somewhere to write a regular file — and is
    sized by `scratch_pvc_size()` rather than at the source capacity: it
    holds a full copy of the same disk as a FILE, and a filesystem's own
    metadata and reserved blocks mean a volume of exactly N bytes cannot hold
    an N-byte file.
    Accepted cost: a Block publish transiently needs roughly twice the disk's
    size (the thin snapshot clone plus this scratch copy).

    `storage_class`/`access_modes`/`storage_size` default to values that keep
    planner-only unit tests working without a live disk to read from; a real
    publish always supplies the source PVC's own values.

    `snapshot_class` names the VolumeSnapshotClass. Empty means "leave the
    field off", which is only correct where a cluster-default class exists;
    the handler resolves one rather than betting on that.
    """
    snap_name, tmp_name = cleanup_names(job_name)
    owner = [
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "name": job_name,
            "uid": job_uid,
            "controller": True,
            "blockOwnerDeletion": False,
        }
    ]
    dependents: list[dict[str, Any]] = [
        {
            "apiVersion": "snapshot.storage.k8s.io/v1",
            "kind": "VolumeSnapshot",
            "metadata": {
                "name": snap_name,
                "namespace": namespace,
                "ownerReferences": owner,
            },
            "spec": {
                "source": {"persistentVolumeClaimName": pvc},
                # Named explicitly when the caller resolved one. Omitting it
                # relies on the cluster having a class annotated as default,
                # which disks.py:create_disk_snapshot already declines to
                # assume — and a snapshot with no class resolves sits Pending
                # with an event and no ApiException, so the publish handler
                # would see success and the Job would time out.
                **({"volumeSnapshotClassName": snapshot_class} if snapshot_class else {}),
            },
        },
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": tmp_name,
                "namespace": namespace,
                "ownerReferences": owner,
            },
            "spec": {
                "accessModes": access_modes or ["ReadWriteOnce"],
                "storageClassName": storage_class,
                "volumeMode": volume_mode,
                "dataSource": {
                    "name": snap_name,
                    "kind": "VolumeSnapshot",
                    "apiGroup": "snapshot.storage.k8s.io",
                },
                "resources": {"requests": {"storage": storage_size}},
            },
        },
    ]

    if volume_mode == "Block":
        dependents.append({
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": scratch_pvc_name(job_name),
                "namespace": namespace,
                "ownerReferences": owner,
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": storage_class,
                "volumeMode": "Filesystem",
                "resources": {"requests": {"storage": scratch_pvc_size(storage_size)}},
            },
        })

    return dependents


async def assert_tag_is_free(
    harbor: Any, token: str, project: str, repository: str, tag: str
) -> None:
    """Raise ValueError if the tag already exists in the catalogue.

    CDI imports a registry source exactly once, so overwriting a tag produces a
    publish that reports success and changes nothing anybody can boot.

    A repository that does not exist yet answers 404, which is the ORDINARY
    case: it is what every first publish to a new repository looks like. That
    is caught here and read as "the tag is free", because it is. Only
    HarborNotFound is caught — a real outage or a rejected token still
    propagates, because neither is evidence the tag is available and
    publishing over an occupied tag is the failure this function exists to
    prevent.
    """
    try:
        artifacts = await harbor.list_artifacts(token, project, repository)
    except HarborNotFound:
        return

    for artifact in artifacts:
        for existing in artifact.get("tags") or []:
            if existing.get("name") == tag:
                raise ValueError(
                    f"tag {tag} already exists in {project}/{repository}; "
                    "publish a new tag rather than replacing one"
                )
