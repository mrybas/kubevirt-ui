"""Planning a snapshot-then-publish.

The VM keeps running: we snapshot its disk, make a temporary PVC from the
snapshot, and let a Job read that. Cross-namespace and same-namespace clones
are both thin on snapshot-capable storage, so the copy costs little.

The plan is data, not actions, so the ordering and the naming can be tested
without a cluster.
"""

from typing import Any

PUBLISH_IMAGE = "gcr.io/go-containerregistry/crane:debug"

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
# (not `/disk`), so `disk` is already that directory and `tar -cf layer.tar
# disk`, run from `/work`, needs nothing else.
_FILESYSTEM_PUSH_SCRIPT = (
    "set -eu; "
    "cd /work && "
    "tar -cf layer.tar disk && "
    'crane auth login "$REGISTRY" -u "$ROBOT_USER" -p "$ROBOT_PASS" && '
    'crane append --oci-empty-base -f layer.tar -t "$REF"'
)

# Block source: there is no filesystem to mount, so the temporary PVC is
# attached as a raw block device instead (`volumeDevices`), and `dd` copies it
# into a regular file — a block device cannot be tarred directly, and crane
# needs a regular file — on a scratch PVC (never emptyDir: a disk this size
# on node ephemeral storage risks evicting unrelated pods through disk
# pressure). The scratch PVC is written to (and the layer built) entirely on
# that persistent volume, not the container's own ephemeral writable layer,
# for the same reason.
_BLOCK_PUSH_SCRIPT = (
    "set -eu; "
    "mkdir -p /scratch/disk && "
    f'dd if="{_BLOCK_DEVICE_PATH}" of=/scratch/disk/disk.img bs=4M && '
    "cd /scratch && "
    "tar -cf layer.tar disk && "
    'crane auth login "$REGISTRY" -u "$ROBOT_USER" -p "$ROBOT_PASS" && '
    'crane append --oci-empty-base -f layer.tar -t "$REF"'
)


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
    active_deadline_seconds: int = 1800,
    volume_mode: str = "Block",
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
    never reaches a terminal phase.
    """
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

    job_name = f"publish-{pvc}"
    tmp_name = cleanup_names(job_name)[1]

    container: dict[str, Any] = {
        "name": "publish",
        "image": PUBLISH_IMAGE,
        "command": ["/bin/sh", "-c"],
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
    `Filesystem` — `dd` needs somewhere to write a regular file — sized the
    same as the temporary PVC, since it holds a full copy of the same disk.
    Accepted cost: a Block publish transiently needs roughly twice the disk's
    size (the thin snapshot clone plus this scratch copy).

    `storage_class`/`access_modes`/`storage_size` default to values that keep
    planner-only unit tests working without a live disk to read from; a real
    publish always supplies the source PVC's own values.
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
            "spec": {"source": {"persistentVolumeClaimName": pvc}},
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
                "resources": {"requests": {"storage": storage_size}},
            },
        })

    return dependents


async def assert_tag_is_free(
    harbor: Any, token: str, project: str, repository: str, tag: str
) -> None:
    """Raise ValueError if the tag already exists in the catalogue.

    CDI imports a registry source exactly once, so overwriting a tag produces a
    publish that reports success and changes nothing anybody can boot.
    """
    for artifact in await harbor.list_artifacts(token, project, repository):
        for existing in artifact.get("tags") or []:
            if existing.get("name") == tag:
                raise ValueError(
                    f"tag {tag} already exists in {project}/{repository}; "
                    "publish a new tag rather than replacing one"
                )
