"""A Block-mode source PVC has no filesystem for volumeMounts to attach to.

Kubernetes requires `volumeDevices`/`devicePath` for a Block volume instead,
and a raw block device cannot be tarred directly — the Job has to `dd` it
into a regular file first. This tests the planners' two branches directly
(no mocked cluster, no FastAPI), the same way the sibling
`test_a_publish_that_fails_cleans_up_after_itself.py` tests the
suspended-Job-as-owner choreography: pure functions, so the ordering and the
per-mode shape can be pinned without a cluster to prove it against.
"""

from app.core.image_publish import publish_dependents, publish_job, scratch_pvc_name


def test_a_block_source_gets_volume_devices_and_no_disk_volume_mount():
    job = publish_job("tenant-a", "ubuntu-disk", "p/u:1", volume_mode="Block")
    container = job["spec"]["template"]["spec"]["containers"][0]

    assert container["volumeDevices"] == [
        {"name": "disk", "devicePath": "/dev/publish-disk"}
    ]
    mounted_names = {vm["name"] for vm in container.get("volumeMounts", [])}
    assert "disk" not in mounted_names


def test_a_filesystem_source_gets_volume_mounts_and_no_volume_devices():
    job = publish_job("tenant-a", "ubuntu-disk", "p/u:1", volume_mode="Filesystem")
    container = job["spec"]["template"]["spec"]["containers"][0]

    assert "volumeDevices" not in container
    assert container["volumeMounts"] == [
        {"name": "disk", "mountPath": "/work/disk", "readOnly": True}
    ]


def test_the_push_script_reads_from_the_device_path_in_the_block_case():
    job = publish_job("tenant-a", "ubuntu-disk", "p/u:1", volume_mode="Block")
    script = " ".join(job["spec"]["template"]["spec"]["containers"][0]["args"])

    assert "/dev/publish-disk" in script


def test_the_push_script_reads_from_the_mount_path_in_the_filesystem_case():
    job = publish_job("tenant-a", "ubuntu-disk", "p/u:1", volume_mode="Filesystem")
    script = " ".join(job["spec"]["template"]["spec"]["containers"][0]["args"])

    assert "cd /work" in script
    assert "/dev/publish-disk" not in script


def test_a_block_source_gets_a_scratch_pvc_among_the_dependents():
    dependents = publish_dependents(
        "tenant-a", "ubuntu-disk", "publish-ubuntu-disk", "uid-1",
        volume_mode="Block",
    )
    kinds = [o["kind"] for o in dependents]
    names = {o["metadata"]["name"] for o in dependents}

    assert kinds.count("PersistentVolumeClaim") == 2
    assert scratch_pvc_name("publish-ubuntu-disk") in names


def test_a_filesystem_source_gets_no_scratch_pvc():
    dependents = publish_dependents(
        "tenant-a", "ubuntu-disk", "publish-ubuntu-disk", "uid-1",
        volume_mode="Filesystem",
    )
    kinds = [o["kind"] for o in dependents]
    names = {o["metadata"]["name"] for o in dependents}

    assert kinds.count("PersistentVolumeClaim") == 1
    assert scratch_pvc_name("publish-ubuntu-disk") not in names


def test_the_scratch_pvc_is_owned_by_the_job_just_like_the_others():
    dependents = publish_dependents(
        "tenant-a", "ubuntu-disk", "publish-ubuntu-disk", "uid-1",
        volume_mode="Block",
    )
    scratch = next(
        o for o in dependents
        if o["metadata"]["name"] == scratch_pvc_name("publish-ubuntu-disk")
    )

    owner = scratch["metadata"]["ownerReferences"][0]
    assert owner["kind"] == "Job"
    assert owner["uid"] == "uid-1"
    assert owner["controller"] is True


def test_the_scratch_pvc_is_filesystem_mode_even_though_the_source_is_block():
    dependents = publish_dependents(
        "tenant-a", "ubuntu-disk", "publish-ubuntu-disk", "uid-1",
        volume_mode="Block",
    )
    scratch = next(
        o for o in dependents
        if o["metadata"]["name"] == scratch_pvc_name("publish-ubuntu-disk")
    )

    assert scratch["spec"]["volumeMode"] == "Filesystem"
