"""Planning a snapshot-then-publish.

The VM keeps running: we snapshot its disk, make a temporary PVC from the
snapshot, and let a Job read that. Cross-namespace and same-namespace clones
are both thin on snapshot-capable storage, so the copy costs little.

The plan is data, not actions, so the ordering and the naming can be tested
without a cluster.
"""

from typing import Any

PUBLISH_IMAGE = "gcr.io/go-containerregistry/crane:debug"

# The lab's existing publish pattern: pack the mounted disk into a single-layer
# OCI image and append it to an empty base — the shape both CDI's
# `source.registry` and KubeVirt's `containerDisk` can consume. Mounted at
# `/work/disk` (not `/disk`) so `tar -cf layer.tar disk`, run from `/work`,
# produces a layer with the disk file under `disk/`, matching that pattern
# exactly rather than a bare disk file at the tar root.
_PUSH_SCRIPT = (
    "set -eu; "
    "cd /work && "
    "tar -cf layer.tar disk && "
    'crane auth login "$REGISTRY" -u "$ROBOT_USER" -p "$ROBOT_PASS" && '
    'crane append --oci-empty-base -f layer.tar -t "$REF"'
)


def cleanup_names(job_name: str) -> tuple[str, str]:
    """Names of the two objects a publish leaves behind if it dies.

    Derived from the Job name so cleanup never has to guess, and never has to
    be told the source disk's name — deleting that would destroy the very disk
    the user asked to publish.
    """
    return f"{job_name}-snap", f"{job_name}-tmp"


def publish_job(
    namespace: str,
    pvc: str,
    ref: str,
    *,
    registry: str = "",
    secret_name: str = "",
    active_deadline_seconds: int = 1800,
) -> dict[str, Any]:
    """The publish Job, created SUSPENDED so it can own its dependents.

    An ownerReference needs the owner's UID, which exists only once the owner
    is created. Creating the snapshot and PVC first would leave them ownerless
    and therefore un-reaped when the Job fails.

    The container actually pushes: it packs the read-only-mounted disk into
    the containerDisk layout and pushes it with crane, authenticating with the
    tenant's robot credential — a Secret in the target namespace holding
    CDI's own `accessKeyId`/`secretKey` shape, the same Secret CDI pulls with.
    Pushing is a registry operation, the one place a robot account works.

    `registry`/`secret_name` default empty so the existing unit tests here,
    which only assert on the Job's shape (suspend flag, naming), do not have
    to supply them. A real publish always passes both — without a registry
    host the push has nowhere to go, and without a secret it has nothing to
    authenticate with.

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

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": f"publish-{pvc}", "namespace": namespace},
        "spec": {
            "suspend": True,
            "backoffLimit": 1,
            "activeDeadlineSeconds": active_deadline_seconds,
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "publish",
                            "image": PUBLISH_IMAGE,
                            "command": ["/bin/sh", "-c"],
                            "args": [_PUSH_SCRIPT],
                            "env": env,
                            "volumeMounts": [
                                {
                                    "name": "disk",
                                    "mountPath": "/work/disk",
                                    "readOnly": True,
                                }
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "disk",
                            "persistentVolumeClaim": {
                                "claimName": cleanup_names(f"publish-{pvc}")[1],
                                "readOnly": True,
                            },
                        }
                    ],
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
    """The snapshot and the temporary PVC, both owned by the Job.

    Ownership is what makes cleanup unconditional: Kubernetes reaps these when
    the Job goes, whether it succeeded, failed, or was deleted by hand.

    The temporary PVC copies `storageClassName`, `volumeMode` and
    `accessModes` from the disk being published — mirroring
    `disks.py`'s `rollback_snapshot` — and asks for a real size rather than a
    placeholder. Most CSI provisioners silently refuse (leave the PVC
    `Pending` forever, no `ApiException` this handler could ever see) a
    request smaller than the snapshot's `restoreSize`. `storage_class`/
    `access_modes`/`storage_size` default to values that keep the existing
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
    return [
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
