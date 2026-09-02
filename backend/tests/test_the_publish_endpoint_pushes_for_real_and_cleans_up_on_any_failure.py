"""POST /images/publish, exercised through the real endpoint.

The neighbouring `test_a_publish_that_fails_cleans_up_after_itself.py` and
`test_a_tag_that_already_exists_is_refused.py` prove the pure planners
(`publish_job`, `publish_dependents`, `assert_tag_is_free`) in isolation,
without FastAPI or a Kubernetes client. Neither proves the *handler*: that the
501 gate, the 422s, and the 409 actually fire over HTTP; that the Job really
is created before its dependents and that the dependents really are owned by
the UID the cluster handed back (not a placeholder); that a failure — of any
kind, not only a Kubernetes ApiException — during that window deletes the Job
rather than leaving it permanently suspended; and that the Job this handler
builds actually contains a push (credentials, a command) rather than just an
idle container. A regression in any of those passes every test in this
file's neighbours and would only show up against a live cluster.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from kubernetes_asyncio.client.rest import ApiException

from app.core.auth import User
from app.core.harbor_client import HarborNotFound, HarborUnauthorized, HarborUnavailable

PUBLISH_BODY = {
    "namespace": "tenant-a",
    "disk_name": "ubuntu-disk",
    "project": "vm-images-tenant-a",
    "repository": "ubuntu-2204",
    "tag": "20260902",
    "secret_name": "harbor-robot-tenant-a",
}


class _FakeApiObject:
    """Stands in for a kubernetes_asyncio generated model (V1Job, V1PVC, ...).

    `create_object` calls `.to_dict()` on whatever BatchV1Api/CoreV1Api hand
    back — a real cluster returns a typed model there, never a plain dict.
    """

    def __init__(self, body: dict) -> None:
        self._body = body

    def to_dict(self) -> dict:
        return self._body


def _fake_source_pvc(
    storage_class: str = "ceph-rbd",
    volume_mode: str = "Block",
    access_modes: list[str] | None = None,
    capacity: str = "20Gi",
) -> SimpleNamespace:
    return SimpleNamespace(
        spec=SimpleNamespace(
            storage_class_name=storage_class,
            volume_mode=volume_mode,
            access_modes=access_modes or ["ReadWriteOnce"],
        ),
        status=SimpleNamespace(capacity={"storage": capacity}),
    )


def _wire_k8s_happy_path(
    mock_k8s_client: MagicMock,
    *,
    job_uid: str = "job-uid-real-123",
    snapshot_status: dict | None = None,
) -> tuple[MagicMock, MagicMock]:
    """A cluster that accepts every write the handler makes.

    Returns (batch_api, custom_api) so a test can inspect exactly what was
    sent to each — the whole point being that mocking away the write also
    means nothing enforces its shape unless a test reads the call back.
    """
    mock_k8s_client.core_api.read_namespaced_secret = AsyncMock(return_value=MagicMock())
    mock_k8s_client.core_api.read_namespaced_persistent_volume_claim = AsyncMock(
        return_value=_fake_source_pvc()
    )
    mock_k8s_client.core_api.create_namespaced_persistent_volume_claim = AsyncMock(
        return_value=_FakeApiObject({"metadata": {"name": "tmp", "namespace": "tenant-a"}})
    )

    batch_api = MagicMock()
    batch_api.create_namespaced_job = AsyncMock(
        return_value=_FakeApiObject({
            "metadata": {
                "name": "publish-ubuntu-disk",
                "namespace": "tenant-a",
                "uid": job_uid,
            },
            "spec": {"suspend": True},
        })
    )
    batch_api.patch_namespaced_job = AsyncMock()
    batch_api.delete_namespaced_job = AsyncMock()

    custom_api = MagicMock()
    custom_api.create_namespaced_custom_object = AsyncMock(
        return_value={
            "metadata": {"name": "publish-ubuntu-disk-snap", "namespace": "tenant-a"},
            "status": snapshot_status or {},
        }
    )

    return batch_api, custom_api


def _patches(batch_api: MagicMock, custom_api: MagicMock):
    return (
        patch("app.api.v1.images.client.BatchV1Api", return_value=batch_api),
        patch("app.api.v1.images.client.CustomObjectsApi", return_value=custom_api),
        patch("app.api.v1.images.harbor_registry_host", return_value="harbor.example"),
    )


def _post(client: TestClient, body: dict | None = None):
    return client.post("/api/v1/images/publish", json={**PUBLISH_BODY, **(body or {})})


@pytest.fixture(autouse=True)
def _a_caller_who_is_actually_signed_in(fake_user: User) -> None:
    """Every test below fires as a caller with a real token.

    `fake_user.raw_token` defaults to None (the AUTH_TYPE=none shape), and
    publishing now refuses that outright: the handler used to send Harbor
    `user.raw_token or ""`, an empty bearer that a public project answers 200
    — so the tag check "passed" while proving nothing. Tests that left the
    token unset were therefore exercising exactly the path that had to go.
    The one test that wants the no-token case asks for it explicitly.
    """
    fake_user.raw_token = "caller-oidc-token"


class TestTheGateAndTheRefusals:
    def test_the_flag_off_returns_501(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("HARBOR_IMAGE_ENABLED", raising=False)

        response = _post(client)

        assert response.status_code == 501

    def test_an_invalid_k8s_name_is_refused_with_422(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")

        response = _post(client, {"namespace": "Not_Valid"})

        assert response.status_code == 422

    def test_a_missing_robot_secret_is_named_in_the_refusal_before_anything_is_created(
        self,
        client: TestClient,
        mock_k8s_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        mock_k8s_client.core_api.read_namespaced_secret = AsyncMock(
            side_effect=ApiException(status=404)
        )
        batch_api = MagicMock()
        batch_api.create_namespaced_job = AsyncMock(
            side_effect=AssertionError("must not create anything past the refusal")
        )

        with patch("app.api.v1.images.client.BatchV1Api", return_value=batch_api):
            response = _post(client)

        assert response.status_code == 422
        assert "harbor-robot-tenant-a" in response.json()["detail"]
        batch_api.create_namespaced_job.assert_not_called()

    def test_a_missing_disk_is_reported_as_404_before_anything_is_created(
        self,
        client: TestClient,
        mock_k8s_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        mock_k8s_client.core_api.read_namespaced_secret = AsyncMock(return_value=MagicMock())
        mock_k8s_client.core_api.read_namespaced_persistent_volume_claim = AsyncMock(
            side_effect=ApiException(status=404)
        )
        batch_api = MagicMock()
        batch_api.create_namespaced_job = AsyncMock(
            side_effect=AssertionError("must not create anything past the refusal")
        )

        with patch("app.api.v1.images.client.BatchV1Api", return_value=batch_api):
            response = _post(client)

        assert response.status_code == 404
        batch_api.create_namespaced_job.assert_not_called()

    def test_an_occupied_tag_is_refused_with_409_before_anything_is_created(
        self,
        client: TestClient,
        mock_k8s_client: MagicMock,
        mock_harbor_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        mock_k8s_client.core_api.read_namespaced_secret = AsyncMock(return_value=MagicMock())
        mock_k8s_client.core_api.read_namespaced_persistent_volume_claim = AsyncMock(
            return_value=_fake_source_pvc()
        )
        mock_harbor_client.list_artifacts = AsyncMock(
            return_value=[{"tags": [{"name": "20260902"}]}]
        )
        batch_api = MagicMock()
        batch_api.create_namespaced_job = AsyncMock(
            side_effect=AssertionError("must not create anything past the refusal")
        )

        with patch("app.api.v1.images.client.BatchV1Api", return_value=batch_api):
            response = _post(client)

        assert response.status_code == 409
        assert "20260902" in response.json()["detail"]
        batch_api.create_namespaced_job.assert_not_called()


class TestTheHappyPathOrderingAndOwnership:
    def test_the_job_is_created_first_and_dependents_are_owned_by_its_real_uid(
        self,
        client: TestClient,
        mock_k8s_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Proves the UID used for ownership is the one the cluster returned.

        A handler that built an ownerReference from a locally-invented UID
        (or from the Job it *sent*, rather than the one it got back) would
        pass every planner-level test and still be wrong — this is the one
        place that distinction is visible.
        """
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        batch_api, custom_api = _wire_k8s_happy_path(
            mock_k8s_client, job_uid="the-real-uid-from-the-cluster"
        )

        p1, p2, p3 = _patches(batch_api, custom_api)
        with p1, p2, p3:
            response = _post(client)

        assert response.status_code == 202
        assert response.json()["job"] == "publish-ubuntu-disk"

        # Job created before any dependent.
        assert batch_api.create_namespaced_job.call_count == 1
        assert custom_api.create_namespaced_custom_object.call_count == 1
        # Two PVC creates: the temporary clone and, because the default fake
        # source disk is Block-mode, the scratch PVC too.
        assert mock_k8s_client.core_api.create_namespaced_persistent_volume_claim.call_count == 2

        snapshot_body = custom_api.create_namespaced_custom_object.call_args.kwargs["body"]
        pvc_calls = mock_k8s_client.core_api.create_namespaced_persistent_volume_claim.call_args_list
        pvc_bodies = [call.kwargs["body"] for call in pvc_calls]
        for owned in (snapshot_body, *pvc_bodies):
            owner = owned["metadata"]["ownerReferences"][0]
            assert owner["kind"] == "Job"
            assert owner["uid"] == "the-real-uid-from-the-cluster"
            assert owner["controller"] is True

        # Unsuspended only after every dependent exists.
        batch_api.patch_namespaced_job.assert_called_once()
        assert batch_api.patch_namespaced_job.call_args.kwargs["body"] == {
            "spec": {"suspend": False}
        }

    def test_the_temporary_pvc_copies_the_sources_class_mode_and_a_real_size(
        self, client: TestClient, mock_k8s_client: MagicMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        batch_api, custom_api = _wire_k8s_happy_path(mock_k8s_client)
        mock_k8s_client.core_api.read_namespaced_persistent_volume_claim = AsyncMock(
            return_value=_fake_source_pvc(
                storage_class="ceph-rbd", volume_mode="Filesystem",
                access_modes=["ReadWriteMany"], capacity="42Gi",
            )
        )

        p1, p2, p3 = _patches(batch_api, custom_api)
        with p1, p2, p3:
            response = _post(client)

        assert response.status_code == 202
        pvc_body = mock_k8s_client.core_api.create_namespaced_persistent_volume_claim.call_args.kwargs["body"]
        spec = pvc_body["spec"]
        assert spec["storageClassName"] == "ceph-rbd"
        assert spec["volumeMode"] == "Filesystem"
        assert spec["accessModes"] == ["ReadWriteMany"]
        # Never "0" — the source disk's own capacity, since the snapshot
        # this test's fake backend created carries no restoreSize yet.
        assert spec["resources"]["requests"]["storage"] == "42Gi"

    def test_the_snapshots_own_restore_size_wins_when_the_backend_already_reports_one(
        self, client: TestClient, mock_k8s_client: MagicMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        batch_api, custom_api = _wire_k8s_happy_path(
            mock_k8s_client, snapshot_status={"restoreSize": "45Gi"}
        )

        p1, p2, p3 = _patches(batch_api, custom_api)
        with p1, p2, p3:
            response = _post(client)

        assert response.status_code == 202
        # The FIRST PVC create is the temporary clone (the one built from the
        # snapshot); a second one — the scratch PVC, since the default fake
        # source disk here is Block-mode — follows it and is sized from the
        # source capacity, not the snapshot's restoreSize.
        first_call = mock_k8s_client.core_api.create_namespaced_persistent_volume_claim.call_args_list[0]
        pvc_body = first_call.kwargs["body"]
        assert pvc_body["spec"]["resources"]["requests"]["storage"] == "45Gi"

    def test_the_job_pushes_with_the_robot_secret_and_never_the_callers_token(
        self,
        client: TestClient,
        mock_k8s_client: MagicMock,
        fake_user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Critical: the Job must actually push, as the robot, never the caller.

        Pins the container's command (crane push, not an idle default
        entrypoint), the credential source (a secretKeyRef naming the
        request's `secret_name`, in CDI's own accessKeyId/secretKey shape —
        never a docker config, never inline creds), and the absence of the
        caller's own OIDC token anywhere in the Job body — that token is for
        Harbor's management API only, and must never reach the registry
        push.
        """
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        fake_user.raw_token = "caller-oidc-token-must-not-leak"
        batch_api, custom_api = _wire_k8s_happy_path(mock_k8s_client)

        p1, p2, p3 = _patches(batch_api, custom_api)
        with p1, p2, p3:
            response = _post(client)

        assert response.status_code == 202
        job_body = batch_api.create_namespaced_job.call_args.kwargs["body"]
        container = job_body["spec"]["template"]["spec"]["containers"][0]

        script = " ".join(container.get("args", []))
        assert "crane" in script
        assert "tar" in script

        env_by_name = {e["name"]: e for e in container["env"]}
        assert env_by_name["ROBOT_USER"]["valueFrom"]["secretKeyRef"] == {
            "name": "harbor-robot-tenant-a", "key": "accessKeyId",
        }
        assert env_by_name["ROBOT_PASS"]["valueFrom"]["secretKeyRef"] == {
            "name": "harbor-robot-tenant-a", "key": "secretKey",
        }
        assert "harbor.example" in env_by_name["REF"]["value"]

        import json
        assert "caller-oidc-token-must-not-leak" not in json.dumps(job_body)

    def test_a_block_source_disk_gets_volume_devices_and_a_scratch_pvc(
        self, client: TestClient, mock_k8s_client: MagicMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Critical 5: a Block PVC has no filesystem for volumeMounts to
        attach to at all — Kubernetes leaves such a Pod stuck in
        ContainerCreating/FailedMount rather than refusing it up front, so
        this has to be right, not just plausible-looking.
        """
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        batch_api, custom_api = _wire_k8s_happy_path(mock_k8s_client)
        mock_k8s_client.core_api.read_namespaced_persistent_volume_claim = AsyncMock(
            return_value=_fake_source_pvc(volume_mode="Block", capacity="20Gi")
        )

        p1, p2, p3 = _patches(batch_api, custom_api)
        with p1, p2, p3:
            response = _post(client)

        assert response.status_code == 202
        job_body = batch_api.create_namespaced_job.call_args.kwargs["body"]
        container = job_body["spec"]["template"]["spec"]["containers"][0]

        assert container["volumeDevices"] == [
            {"name": "disk", "devicePath": "/dev/publish-disk"}
        ]
        # No volumeMounts entry names the "disk" volume — only the scratch
        # PVC is mounted as a filesystem.
        mounted_names = {vm["name"] for vm in container.get("volumeMounts", [])}
        assert "disk" not in mounted_names
        assert "scratch" in mounted_names

        script = " ".join(container.get("args", []))
        assert "/dev/publish-disk" in script
        assert "dd " in script or "dd if=" in script

        # The scratch PVC is among the dependents, owned by the Job like the
        # others, and created (cleanup still covers everything).
        pvc_calls = mock_k8s_client.core_api.create_namespaced_persistent_volume_claim.call_args_list
        pvc_bodies = [call.kwargs["body"] for call in pvc_calls]
        scratch_bodies = [b for b in pvc_bodies if b["metadata"]["name"].endswith("-scratch")]
        assert len(scratch_bodies) == 1
        scratch_body = scratch_bodies[0]
        assert scratch_body["spec"]["volumeMode"] == "Filesystem"
        owner = scratch_body["metadata"]["ownerReferences"][0]
        assert owner["kind"] == "Job"
        assert owner["controller"] is True

    def test_a_filesystem_source_disk_gets_volume_mounts_and_no_scratch_pvc(
        self, client: TestClient, mock_k8s_client: MagicMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        batch_api, custom_api = _wire_k8s_happy_path(mock_k8s_client)
        mock_k8s_client.core_api.read_namespaced_persistent_volume_claim = AsyncMock(
            return_value=_fake_source_pvc(volume_mode="Filesystem", capacity="20Gi")
        )

        p1, p2, p3 = _patches(batch_api, custom_api)
        with p1, p2, p3:
            response = _post(client)

        assert response.status_code == 202
        job_body = batch_api.create_namespaced_job.call_args.kwargs["body"]
        container = job_body["spec"]["template"]["spec"]["containers"][0]

        assert "volumeDevices" not in container
        assert container["volumeMounts"] == [
            {"name": "disk", "mountPath": "/work/disk", "readOnly": True}
        ]

        script = " ".join(container.get("args", []))
        assert "cd /work" in script
        assert "dd " not in script and "dd if=" not in script

        # Exactly one PVC create — no scratch PVC for a Filesystem source.
        assert mock_k8s_client.core_api.create_namespaced_persistent_volume_claim.call_count == 1


class TestCleanupOnFailure:
    def test_an_api_failure_creating_a_dependent_deletes_the_job_and_reports_422(
        self, client: TestClient, mock_k8s_client: MagicMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        batch_api, custom_api = _wire_k8s_happy_path(mock_k8s_client)
        custom_api.create_namespaced_custom_object = AsyncMock(
            side_effect=ApiException(status=500, reason="etcd is unavailable")
        )

        p1, p2, p3 = _patches(batch_api, custom_api)
        with p1, p2, p3:
            response = _post(client)

        assert response.status_code == 422
        batch_api.delete_namespaced_job.assert_called_once()
        assert batch_api.delete_namespaced_job.call_args.kwargs["name"] == "publish-ubuntu-disk"
        # Never unsuspended — it stays suspended only up to the deletion.
        batch_api.patch_namespaced_job.assert_not_called()

    def test_a_non_api_failure_still_deletes_the_job_and_propagates(
        self, client: TestClient, mock_k8s_client: MagicMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Important 3: cleanup must not be conditional on `ApiException`.

        A timeout or connection reset while creating a dependent is exactly
        as capable of leaving a permanently suspended (and therefore never
        reaped) Job as a Kubernetes 500 is — a `try/except ApiException`
        alone lets this one through.
        """
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        batch_api, custom_api = _wire_k8s_happy_path(mock_k8s_client)
        custom_api.create_namespaced_custom_object = AsyncMock(
            side_effect=TimeoutError("connection reset")
        )

        p1, p2, p3 = _patches(batch_api, custom_api)
        with p1, p2, p3:
            with pytest.raises(TimeoutError):
                _post(client)

        batch_api.delete_namespaced_job.assert_called_once()
        assert batch_api.delete_namespaced_job.call_args.kwargs["name"] == "publish-ubuntu-disk"

    def test_a_failure_deleting_the_job_during_rollback_does_not_hide_the_original_error(
        self, client: TestClient, mock_k8s_client: MagicMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        batch_api, custom_api = _wire_k8s_happy_path(mock_k8s_client)
        custom_api.create_namespaced_custom_object = AsyncMock(
            side_effect=ApiException(status=500, reason="etcd is unavailable")
        )
        batch_api.delete_namespaced_job = AsyncMock(
            side_effect=ApiException(status=500, reason="delete also failed")
        )

        p1, p2, p3 = _patches(batch_api, custom_api)
        with p1, p2, p3:
            response = _post(client)

        # The original creation failure is what the caller sees, not the
        # secondary failure to clean it up.
        assert response.status_code == 422
        assert "etcd is unavailable" in response.json()["detail"]


class TestWhatHarborsOwnFailuresLookLikeToTheCaller:
    """C3: `assert_tag_is_free` calls Harbor, and Harbor has its own failures.

    The handler used to catch `ValueError` only. Neither designed Harbor
    exception was caught and there was no outer try, so every one of these
    was a 500 — including the one that is not a failure at all: a repository
    that does not exist yet, which is what EVERY first publish to a new
    repository looks like.
    """

    def test_a_repository_that_does_not_exist_yet_publishes_normally(
        self,
        client: TestClient,
        mock_k8s_client: MagicMock,
        mock_harbor_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The ordinary case: nothing has ever been pushed to this repository.

        Harbor answers the artifact listing with 404. Read as an outage that
        is a guaranteed 500 on the first publish of every new image, which is
        the most common publish there is.
        """
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        batch_api, custom_api = _wire_k8s_happy_path(mock_k8s_client)
        mock_harbor_client.list_artifacts = AsyncMock(
            side_effect=HarborNotFound("Harbor returned 404 for /artifacts")
        )

        p1, p2, p3 = _patches(batch_api, custom_api)
        with p1, p2, p3:
            response = _post(client)

        assert response.status_code == 202
        batch_api.create_namespaced_job.assert_called_once()

    def test_an_unreachable_harbor_is_a_502_not_a_500(
        self,
        client: TestClient,
        mock_k8s_client: MagicMock,
        mock_harbor_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Not the caller's fault, and nothing is created.

        Publishing without a working tag check would mean pushing over a tag
        that may already exist — which CDI never re-imports, so the image
        would look published and be unbootable.
        """
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        batch_api, custom_api = _wire_k8s_happy_path(mock_k8s_client)
        mock_harbor_client.list_artifacts = AsyncMock(
            side_effect=HarborUnavailable("no route to host")
        )

        p1, p2, p3 = _patches(batch_api, custom_api)
        with p1, p2, p3:
            response = _post(client)

        assert response.status_code == 502
        batch_api.create_namespaced_job.assert_not_called()

    def test_a_rejected_identity_is_a_403_not_a_500(
        self,
        client: TestClient,
        mock_k8s_client: MagicMock,
        mock_harbor_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        batch_api, custom_api = _wire_k8s_happy_path(mock_k8s_client)
        mock_harbor_client.verify_identity = AsyncMock(
            side_effect=HarborUnauthorized("Harbor rejected the caller's identity")
        )

        p1, p2, p3 = _patches(batch_api, custom_api)
        with p1, p2, p3:
            response = _post(client)

        assert response.status_code == 403
        batch_api.create_namespaced_job.assert_not_called()

    def test_the_identity_is_verified_before_the_catalogue_is_read(
        self,
        client: TestClient,
        mock_k8s_client: MagicMock,
        mock_harbor_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Harbor's list endpoints answer 200 for any bearer.

        So walking an artifact list proves nothing about who is walking it: a
        garbage token sees an empty list, finds the tag "free", and publishes
        over whatever is really there. `verify_identity` is the only call that
        actually refuses a wrong identity, and it has to come first.
        """
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        batch_api, custom_api = _wire_k8s_happy_path(mock_k8s_client)
        order: list[str] = []
        mock_harbor_client.verify_identity = AsyncMock(
            side_effect=lambda *a, **k: order.append("verify")
        )
        mock_harbor_client.list_artifacts = AsyncMock(
            side_effect=lambda *a, **k: (order.append("list"), [])[1]
        )

        p1, p2, p3 = _patches(batch_api, custom_api)
        with p1, p2, p3:
            response = _post(client)

        assert response.status_code == 202
        assert order == ["verify", "list"]

    def test_the_callers_own_token_is_what_reaches_harbor(
        self,
        client: TestClient,
        mock_k8s_client: MagicMock,
        mock_harbor_client: MagicMock,
        fake_user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Not an empty string. `user.raw_token or ""` was back here."""
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        fake_user.raw_token = "the-callers-own-token"
        batch_api, custom_api = _wire_k8s_happy_path(mock_k8s_client)

        p1, p2, p3 = _patches(batch_api, custom_api)
        with p1, p2, p3:
            response = _post(client)

        assert response.status_code == 202
        assert mock_harbor_client.verify_identity.call_args.args[0] == (
            "the-callers-own-token"
        )
        assert mock_harbor_client.list_artifacts.call_args.args[0] == (
            "the-callers-own-token"
        )

    def test_no_token_at_all_is_refused_rather_than_sent_as_an_empty_bearer(
        self,
        client: TestClient,
        mock_k8s_client: MagicMock,
        mock_harbor_client: MagicMock,
        fake_user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AUTH_TYPE=none produces a User with no raw_token.

        An empty bearer is answered 200 by a public Harbor project, so the
        tag check would "pass" having checked nothing at all.
        """
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        fake_user.raw_token = None
        batch_api, custom_api = _wire_k8s_happy_path(mock_k8s_client)

        p1, p2, p3 = _patches(batch_api, custom_api)
        with p1, p2, p3:
            response = _post(client)

        assert response.status_code == 401
        mock_harbor_client.list_artifacts.assert_not_called()
        batch_api.create_namespaced_job.assert_not_called()


class TestTheJobNameIsUniqueAndBounded:
    """I3: `publish-{pvc}` collided and could exceed the 63-char Job cap."""

    def test_a_long_disk_name_still_produces_a_creatable_job_name(
        self, client: TestClient, mock_k8s_client: MagicMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A PVC may be 253 characters; a Job name may be 63.

        The API server refuses the create outright — which arrives as an
        unhandled exception, not as anything the user can read.
        """
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        long_name = "a" * 120
        batch_api, custom_api = _wire_k8s_happy_path(mock_k8s_client)

        p1, p2, p3 = _patches(batch_api, custom_api)
        with p1, p2, p3:
            response = _post(client, {"disk_name": long_name})

        assert response.status_code == 202
        job_body = batch_api.create_namespaced_job.call_args.kwargs["body"]
        assert len(job_body["metadata"]["name"]) <= 63

    def test_publishing_the_same_disk_twice_does_not_reuse_the_name(
        self, client: TestClient, mock_k8s_client: MagicMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ttlSecondsAfterFinished keeps a succeeded Job for an hour.

        A name derived from the disk alone therefore makes the second publish
        of the same disk an AlreadyExists — a failure the user cannot act on.
        """
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        batch_api, custom_api = _wire_k8s_happy_path(mock_k8s_client)

        p1, p2, p3 = _patches(batch_api, custom_api)
        with p1, p2, p3:
            assert _post(client).status_code == 202
            assert _post(client).status_code == 202

        names = [
            call.kwargs["body"]["metadata"]["name"]
            for call in batch_api.create_namespaced_job.call_args_list
        ]
        assert names[0] != names[1]

    def test_a_name_collision_surfaces_as_409_not_an_unhandled_500(
        self, client: TestClient, mock_k8s_client: MagicMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """This create sits outside the rollback try — there is nothing to
        roll back yet — so nothing else would catch it."""
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        batch_api, custom_api = _wire_k8s_happy_path(mock_k8s_client)
        batch_api.create_namespaced_job = AsyncMock(
            side_effect=ApiException(status=409, reason="AlreadyExists")
        )

        p1, p2, p3 = _patches(batch_api, custom_api)
        with p1, p2, p3:
            response = _post(client)

        assert response.status_code == 409
        assert "ubuntu-disk" in response.json()["detail"]


class TestTheScratchPvcCanHoldWhatIsWrittenToIt:
    """I2: `dd` writes the full source capacity into a FILE on this PVC."""

    def test_the_scratch_pvc_is_larger_than_the_source_capacity(
        self, client: TestClient, mock_k8s_client: MagicMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ext4 metadata plus reserved blocks leave ~0.93-0.95 usable.

        Sized 1:1 with the source, a full copy ENOSPCs partway through — at
        the end of the slowest step, after the copy has already run.
        """
        from app.core.image_publish import _parse_quantity

        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        batch_api, custom_api = _wire_k8s_happy_path(mock_k8s_client)
        mock_k8s_client.core_api.read_namespaced_persistent_volume_claim = AsyncMock(
            return_value=_fake_source_pvc(volume_mode="Block", capacity="100Gi")
        )

        p1, p2, p3 = _patches(batch_api, custom_api)
        with p1, p2, p3:
            response = _post(client)

        assert response.status_code == 202
        pvc_bodies = [
            call.kwargs["body"]
            for call in mock_k8s_client.core_api.create_namespaced_persistent_volume_claim.call_args_list
        ]
        scratch = next(b for b in pvc_bodies if b["metadata"]["name"].endswith("-scratch"))
        asked = scratch["spec"]["resources"]["requests"]["storage"]

        assert _parse_quantity(asked) > _parse_quantity("100Gi")

    def test_the_temporary_clone_is_still_sized_at_the_source_capacity(
        self, client: TestClient, mock_k8s_client: MagicMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The margin belongs to the scratch only — the clone is a block-level
        restore of the snapshot, not a file written into a filesystem."""
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", "true")
        batch_api, custom_api = _wire_k8s_happy_path(mock_k8s_client)
        mock_k8s_client.core_api.read_namespaced_persistent_volume_claim = AsyncMock(
            return_value=_fake_source_pvc(volume_mode="Block", capacity="100Gi")
        )

        p1, p2, p3 = _patches(batch_api, custom_api)
        with p1, p2, p3:
            response = _post(client)

        assert response.status_code == 202
        first = mock_k8s_client.core_api.create_namespaced_persistent_volume_claim.call_args_list[0]
        assert first.kwargs["body"]["spec"]["resources"]["requests"]["storage"] == "100Gi"
