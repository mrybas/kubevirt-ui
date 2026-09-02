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


def test_the_shell_the_job_invokes_actually_exists_in_the_image():
    """The Job's `command` must resolve inside `PUBLISH_IMAGE`'s own userland.

    `gcr.io/go-containerregistry/crane:debug` has no `/bin/sh` at all — its
    shell lives at `/busybox/sh`, reachable as bare `sh` through PATH. A
    hardcoded `/bin/sh` left every publish Job unable to start
    (CreateContainerError, before a single line of the push script ran),
    invisible to every test here because a mocked Kubernetes API never
    execs anything — the object is well-formed and the mock accepts it
    either way. This assertion fails the moment `/bin/sh` (or any other
    absolute path this image lacks) reappears in `command`, in either mode.
    """
    for volume_mode in ("Block", "Filesystem"):
        job = publish_job("tenant-a", "ubuntu-disk", "p/u:1", volume_mode=volume_mode)
        container = job["spec"]["template"]["spec"]["containers"][0]

        assert container["command"][0] != "/bin/sh"
        # Bare `sh`, resolved through PATH — not any other absolute path
        # either, since this image's whole userland is BusyBox under
        # `/busybox/`, not a conventional FHS layout.
        assert not container["command"][0].startswith("/")


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


def test_the_block_push_streams_the_archive_and_never_materialises_a_layer_file():
    """Peak scratch usage must stay at one copy of the disk, not two.

    `dd` already writes a full-size copy to the scratch PVC. Writing
    `tar -cf layer.tar disk` there too — beside the file it is reading —
    means both are alive at once: the scratch PVC is sized for a single
    copy, so a second, uncompressed one is an ENOSPC partway through `tar`,
    not a clean failure. Piping `tar`'s output straight into `crane append
    -f -` (crane reads a tarball from stdin — see `pkg/crane/append.go`'s
    `streamFile`, which special-cases `path == "-"` to `os.Stdin` and wraps
    it in a real streaming layer) avoids the second copy entirely.
    """
    job = publish_job("tenant-a", "ubuntu-disk", "p/u:1", volume_mode="Block")
    script = " ".join(job["spec"]["template"]["spec"]["containers"][0]["args"])

    assert "layer.tar" not in script
    assert "tar -cf - disk" in script
    assert "crane append" in script
    assert "-f -" in script
    # The pipe's default exit status (under plain `set -e`) is the last
    # command's — without pipefail a `tar` that died output-starved would
    # not fail the script at all.
    assert "pipefail" in script


def test_the_filesystem_push_streams_too_and_never_stages_a_layer_on_the_node():
    """This test used to pin the OPPOSITE, and pinning it was the bug.

    Its earlier form asserted `layer.tar` was still written, on the grounds
    that the streaming fix was scoped to the Block branch. But `/work` on the
    Filesystem branch is the container's own writable layer — node EPHEMERAL
    storage — so what it was protecting was a full-size tar of a VM disk
    written to the node's filesystem. That is the exact failure the Block
    branch refused an emptyDir to avoid (a 100GB write evicting unrelated
    pods through node disk pressure), and it was worse here, because the
    Block branch at least wrote to a PVC.

    "Unchanged" is not the property that mattered; "does not fill the node"
    is. So the assertion is inverted deliberately: no intermediate file, the
    archive goes straight into crane's stdin.
    """
    job = publish_job("tenant-a", "ubuntu-disk", "p/u:1", volume_mode="Filesystem")
    script = " ".join(job["spec"]["template"]["spec"]["containers"][0]["args"])

    assert "layer.tar" not in script
    assert "tar -cf - disk | crane append" in script
    # pipefail is load-bearing on a pipeline: without it the script's exit
    # status is crane's, so a tar that died mid-stream would publish a
    # truncated image that looks valid and boots to garbage.
    assert "-o pipefail" in script
    # Still no scratch PVC for this branch — nothing is materialised at all.
    assert "/scratch" not in script


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
