"""Planning a snapshot-then-publish.

The VM keeps running: we snapshot its disk, make a temporary PVC from the
snapshot, and let a Job read that. Cross-namespace and same-namespace clones
are both thin on snapshot-capable storage, so the copy costs little.

The plan is data, not actions, so the ordering and the naming can be tested
without a cluster.
"""

from typing import Any

PUBLISH_IMAGE = "gcr.io/go-containerregistry/crane:debug"


def cleanup_names(job_name: str) -> tuple[str, str]:
    """Names of the two objects a publish leaves behind if it dies.

    Derived from the Job name so cleanup never has to guess, and never has to
    be told the source disk's name — deleting that would destroy the very disk
    the user asked to publish.
    """
    return f"{job_name}-snap", f"{job_name}-tmp"


def publish_job(namespace: str, pvc: str, ref: str) -> dict[str, Any]:
    """The publish Job, created SUSPENDED so it can own its dependents.

    An ownerReference needs the owner's UID, which exists only once the owner
    is created. Creating the snapshot and PVC first would leave them ownerless
    and therefore un-reaped when the Job fails.
    """
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": f"publish-{pvc}", "namespace": namespace},
        "spec": {
            "suspend": True,
            "backoffLimit": 1,
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "publish",
                            "image": PUBLISH_IMAGE,
                            "env": [{"name": "REF", "value": ref}],
                            "volumeMounts": [
                                {"name": "disk", "mountPath": "/disk", "readOnly": True}
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
    namespace: str, pvc: str, job_name: str, job_uid: str
) -> list[dict[str, Any]]:
    """The snapshot and the temporary PVC, both owned by the Job.

    Ownership is what makes cleanup unconditional: Kubernetes reaps these when
    the Job goes, whether it succeeded, failed, or was deleted by hand.
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
                "accessModes": ["ReadWriteOnce"],
                "dataSource": {
                    "name": snap_name,
                    "kind": "VolumeSnapshot",
                    "apiGroup": "snapshot.storage.k8s.io",
                },
                "resources": {"requests": {"storage": "0"}},
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
