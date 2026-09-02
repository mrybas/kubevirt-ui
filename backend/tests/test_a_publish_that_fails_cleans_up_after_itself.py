"""A failed publish must not leave a snapshot and a PVC behind.

Orphans here are invisible until the storage pool fills, which is the worst
time to discover them.
"""

from app.core.image_publish import cleanup_names, publish_dependents, publish_job


def test_the_job_starts_suspended_so_it_can_own_what_it_waits_for():
    job = publish_job("tenant-a", "ubuntu-disk", "p/u:1")

    assert job["spec"]["suspend"] is True


def test_the_snapshot_comes_before_the_pvc_that_is_made_from_it():
    kinds = [
        o["kind"]
        for o in publish_dependents("tenant-a", "ubuntu-disk", "publish-ubuntu-disk", "uid-1")
    ]

    assert kinds.index("VolumeSnapshot") < kinds.index("PersistentVolumeClaim")


def test_the_temporary_pvc_is_made_from_the_snapshot_not_the_live_disk():
    dependents = publish_dependents("tenant-a", "ubuntu-disk", "publish-ubuntu-disk", "uid-1")
    pvc = next(o for o in dependents if o["kind"] == "PersistentVolumeClaim")

    assert pvc["spec"]["dataSource"]["kind"] == "VolumeSnapshot"


def test_both_dependents_are_owned_by_the_job_so_kubernetes_reaps_them():
    """A request handler cannot clean up after a Job that fails later."""
    dependents = publish_dependents("tenant-a", "ubuntu-disk", "publish-ubuntu-disk", "uid-1")

    for obj in dependents:
        owner = obj["metadata"]["ownerReferences"][0]
        assert owner["kind"] == "Job"
        assert owner["uid"] == "uid-1"
        assert owner["controller"] is True


def test_every_created_object_is_named_after_the_job_that_owns_it():
    """Cleanup keys off the Job name, so the names must be derivable from it."""
    job = publish_job("tenant-a", "ubuntu-disk", "p/u:1")
    name = job["metadata"]["name"]
    snap, tmp = cleanup_names(name)

    names = {o["metadata"]["name"] for o in publish_dependents("tenant-a", "ubuntu-disk", name, "uid-1")}
    assert snap in names
    assert tmp in names


def test_the_source_disk_is_never_named_as_a_thing_to_delete():
    snap, tmp = cleanup_names("publish-ubuntu-disk-20260902")

    assert "ubuntu-disk" not in (snap, tmp)
