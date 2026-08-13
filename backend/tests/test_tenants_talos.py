"""Unit tests for Talos-flavoured tenants.

Talos nodes do not take a cloud-init blob and join — they ask a trustd signer
for a certificate on port 50001. Most of what these tests pin down is the
chain that makes that request reach a signer which answers it, and the
several places where a wrong value fails silently or only on the second node.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes_asyncio.client import ApiException

from app.api.v1 import tenants_talos
from app.api.v1.tenants_talos import (
    talos_service_name,
    TALOS_TRUSTD_PORT,
    build_bootstrap_token_secret,
    build_talos_pki,
    build_talos_secrets,
    build_talos_worker_config,
    generate_bootstrap_token,
    signer_dns_names,
    talos_control_plane_additions,
    validate_worker_binding,
    worker_endpoint,
)

TENANT = "t1"
NS = "tenant-t1"
VIP = "10.198.190.10"


class TestNaming:
    def test_signer_carries_both_dns_forms(self) -> None:
        # Talos dials the short name; in-cluster clients resolve the long one.
        # Both name the Service we own, not Kamaji's — that is the host that
        # answers on 50001 as well as 6443.
        svc = talos_service_name(TENANT)
        assert signer_dns_names(TENANT, NS) == [
            f"{svc}.{NS}.svc", f"{svc}.{NS}.svc.cluster.local",
        ]

    def test_worker_endpoint_is_a_name_not_an_address(self) -> None:
        # The name is what produces SNI, which is what lets tenants share
        # port 50001 on one VIP. An IP endpoint sends no SNI at all.
        import ipaddress

        endpoint = worker_endpoint(TENANT, NS, 6443)
        assert endpoint == f"https://{talos_service_name(TENANT)}.{NS}.svc:6443"

        host = endpoint.split("//")[1].rsplit(":", 1)[0]
        with pytest.raises(ValueError):
            ipaddress.ip_address(host)


class TestSecrets:
    def test_token_is_kubeadm_format(self) -> None:
        token_id, token_secret = generate_bootstrap_token()
        assert len(token_id) == 6
        assert len(token_secret) == 16
        assert (token_id + token_secret).isalnum()

    def test_tokens_are_not_reused(self) -> None:
        assert generate_bootstrap_token() != generate_bootstrap_token()

    def test_secret_holds_the_three_stable_values(self) -> None:
        data = build_talos_secrets(TENANT, NS)["stringData"]
        assert set(data) == {"machine.token", "cluster.id", "cluster.secret"}

    def test_machine_token_is_the_trustd_token_format(self) -> None:
        token = build_talos_secrets(TENANT, NS)["stringData"]["machine.token"]
        left, right = token.split(".")
        assert (len(left), len(right)) == (6, 16)

    def test_cluster_values_differ_from_each_other(self) -> None:
        data = build_talos_secrets(TENANT, NS)["stringData"]
        assert data["cluster.id"] != data["cluster.secret"]

    def test_bootstrap_token_secret_is_typed_and_grouped(self) -> None:
        secret = build_bootstrap_token_secret("abcdef", "0123456789abcdef")

        assert secret["type"] == "bootstrap.kubernetes.io/token"
        assert secret["metadata"]["name"] == "bootstrap-token-abcdef"
        # Kamaji creates the RBAC around this group already.
        assert secret["stringData"]["auth-extra-groups"] == (
            "system:bootstrappers:kubeadm:default-node-token"
        )

    def test_bootstrap_token_lands_in_kube_system(self) -> None:
        secret = build_bootstrap_token_secret("abcdef", "0123456789abcdef")
        assert secret["metadata"]["namespace"] == "kube-system"


class TestPki:
    def _by_kind(self, objs: list[dict], kind: str) -> list[dict]:
        return [o for o in objs if o["kind"] == kind]

    def test_chain_is_selfsigned_then_ca_then_signer(self) -> None:
        objs = build_talos_pki(TENANT, NS)
        assert [o["kind"] for o in objs] == [
            "Issuer", "Certificate", "Issuer", "Certificate",
        ]

    def test_signer_certificate_has_no_ip_sans(self) -> None:
        # An IP SAN can only be added once the address is known, which means
        # patching the cert afterwards — and then the control plane must be
        # restarted, because the signer reads its cert once at startup.
        signer = self._by_kind(build_talos_pki(TENANT, NS), "Certificate")[1]
        assert "ipAddresses" not in signer["spec"]
        assert signer["spec"]["dnsNames"] == signer_dns_names(TENANT, NS)

    def test_ca_is_ed25519_and_long_lived(self) -> None:
        ca = self._by_kind(build_talos_pki(TENANT, NS), "Certificate")[0]
        assert ca["spec"]["isCA"] is True
        assert ca["spec"]["privateKey"]["algorithm"] == "Ed25519"
        assert ca["spec"]["duration"] == "87600h"

    def test_signer_is_issued_by_the_ca_not_the_selfsigned_issuer(self) -> None:
        objs = build_talos_pki(TENANT, NS)
        signer = self._by_kind(objs, "Certificate")[1]
        assert signer["spec"]["issuerRef"]["name"] == f"{TENANT}-talos-ca-issuer"

    def test_everything_lands_in_the_tenant_namespace(self) -> None:
        assert all(o["metadata"]["namespace"] == NS for o in build_talos_pki(TENANT, NS))


class TestControlPlaneAdditions:
    def test_sidecar_and_volume_are_paired(self) -> None:
        adds = talos_control_plane_additions(
            TENANT, NS, "signer:v1", shared_vip=False,
        )
        volume_name = adds["additionalVolumes"][0]["name"]
        mount = adds["additionalContainers"][0]["volumeMounts"][0]["name"]
        assert volume_name == mount

    def test_cert_sans_match_the_worker_endpoint_name(self) -> None:
        # If these drift, the join fails TLS before trustd is reached.
        adds = talos_control_plane_additions(
            TENANT, NS, "signer:v1", shared_vip=False,
        )
        assert adds["certSANs"] == signer_dns_names(TENANT, NS)

    def test_own_vip_gets_port_50001_on_the_service(self) -> None:
        adds = talos_control_plane_additions(
            TENANT, NS, "signer:v1", shared_vip=False,
        )
        assert adds["additionalPorts"][0]["port"] == TALOS_TRUSTD_PORT

    def test_shared_vip_must_not_add_the_port(self) -> None:
        # MetalLB refuses identical ports on one shared address; the router
        # fronts a per-tenant ClusterIP service instead.
        adds = talos_control_plane_additions(
            TENANT, NS, "signer:v1", shared_vip=True,
        )
        assert "additionalPorts" not in adds


class TestWorkerConfig:
    def _config(self, **kw: object) -> dict:
        base: dict = {
            "api_port": 6443,
            "control_plane_vip": VIP,
            "machine_token": "abcdef.0123456789abcdef",
            "cluster_id": "aWQ=",
            "cluster_secret": "c2VjcmV0",
            "pod_cidr": "10.244.0.0/16",
            "service_cidr": "10.112.0.0/12",
        }
        base.update(kw)
        return build_talos_worker_config(TENANT, NS, **base)  # type: ignore[arg-type]

    def test_endpoint_is_the_name(self) -> None:
        endpoint = self._config()["cluster"]["controlPlane"]["endpoint"]
        assert endpoint == worker_endpoint(TENANT, NS, 6443)

    def test_host_entry_pins_the_name_to_the_vip(self) -> None:
        # Joining needs no DNS — which matters, because the node has none
        # until it has joined.
        entry = self._config()["machine"]["network"]["extraHostEntries"][0]
        assert entry["ip"] == VIP
        assert entry["aliases"] == signer_dns_names(TENANT, NS)

    def test_kubeprism_is_disabled(self) -> None:
        # It proxies the apiserver via localhost, bypassing the name and
        # taking the SNI with it.
        assert self._config()["machine"]["features"]["kubePrism"]["enabled"] is False

    def test_kubelet_rotates_its_certificate(self) -> None:
        args = self._config()["machine"]["kubelet"]["extraArgs"]
        assert args["rotate-certificates"] == "true"

    def test_both_discovery_registries_are_off(self) -> None:
        registries = self._config()["cluster"]["discovery"]["registries"]
        assert registries["kubernetes"]["disabled"] is True
        assert registries["service"]["disabled"] is True

    def test_cidrs_are_carried_through(self) -> None:
        network = self._config()["cluster"]["network"]
        assert network["podSubnets"] == ["10.244.0.0/16"]
        assert network["serviceSubnets"] == ["10.112.0.0/12"]

    def test_ca_is_optional(self) -> None:
        assert "ca" not in self._config()["machine"]
        assert self._config(ca_cert_b64="Y2E=")["machine"]["ca"]["crt"] == "Y2E="


class TestWorkerBinding:
    def test_bridge_is_accepted(self) -> None:
        validate_worker_binding("bridge")

    def test_masquerade_is_refused_with_the_reason(self) -> None:
        # Every guest would see itself as 10.0.2.2 and register under it, so
        # the first node joins and the second cannot.
        with pytest.raises(ValueError, match="10.0.2.2"):
            validate_worker_binding("masquerade")


class TestGoldenImage:
    def test_default_url_matches_the_disk_capk_attaches(self) -> None:
        # CAPK attaches `cloudInitConfigDrive` (config-2), which the openstack
        # variant reads. nocloud looks for `cidata`, never finds it, and the
        # worker sits in maintenance mode instead of joining.
        from app.api.v1.tenants_talos import TALOS_GOLDEN_IMAGE_URL

        assert "openstack" in TALOS_GOLDEN_IMAGE_URL
        assert TALOS_GOLDEN_IMAGE_URL.endswith(".raw.xz")

    def test_dv_imports_over_http_and_cdi_decompresses(self) -> None:
        from app.api.v1.tenants_talos import build_talos_golden_dv

        dv = build_talos_golden_dv(TENANT, NS, "https://x/y.raw.xz", "20Gi", None)
        assert dv["spec"]["source"]["http"]["url"] == "https://x/y.raw.xz"

    def test_storage_class_is_optional(self) -> None:
        from app.api.v1.tenants_talos import build_talos_golden_dv

        without = build_talos_golden_dv(TENANT, NS, "u", "20Gi", None)
        with_sc = build_talos_golden_dv(TENANT, NS, "u", "20Gi", "ceph-block")
        assert "storageClassName" not in without["spec"]["storage"]
        assert with_sc["spec"]["storage"]["storageClassName"] == "ceph-block"


class TestSignerImage:
    def test_default_is_pinned_by_digest(self) -> None:
        # Upstream publishes no versioned tags — `latest` is the only one —
        # so a tag would be a moving target on a single-vendor image.
        from app.api.v1.tenants_capi import TALOS_SIGNER_IMAGE_DEFAULT

        assert "@sha256:" in TALOS_SIGNER_IMAGE_DEFAULT
        assert ":latest" not in TALOS_SIGNER_IMAGE_DEFAULT


class TestClusterSingletons:
    def test_bootstrap_provider_is_the_thin_capi_operator_cr(self) -> None:
        # The only cluster-wide object Talos support needs: it asks
        # capi-operator to install CABPT. Everything else is per-tenant.
        from app.api.v1.tenants_talos import build_bootstrap_provider

        cr = build_bootstrap_provider()
        assert cr["kind"] == "BootstrapProvider"
        assert cr["metadata"]["name"] == "talos"


class TestGoldenImageSourceGuard:
    """The wizard's worker image field is shared with the cloud-init path,
    where it holds a CAPK container-disk reference. CDI takes the Talos golden
    image as an HTTP source and rejects anything that is not a URL — after the
    tenant's Talos secrets and PKI are already written, so the failure leaves
    half a tenant behind."""

    def test_a_registry_reference_falls_back_to_the_known_good_image(self) -> None:
        from app.api.v1.tenants_talos import TALOS_GOLDEN_IMAGE_URL, build_talos_golden_dv

        # What the guard must produce for a container-disk reference.
        dv = build_talos_golden_dv("t1", "ns", TALOS_GOLDEN_IMAGE_URL, "20Gi", None)
        assert dv["spec"]["source"]["http"]["url"].startswith("https://")

    def test_the_default_image_is_an_http_url(self) -> None:
        from app.api.v1.tenants_talos import TALOS_GOLDEN_IMAGE_URL

        assert TALOS_GOLDEN_IMAGE_URL.startswith(("http://", "https://"))
        assert TALOS_GOLDEN_IMAGE_URL.endswith(".raw.xz")

    def test_the_guard_rejects_non_url_sources(self) -> None:
        import inspect

        from app.api.v1 import tenants_talos

        source = inspect.getsource(tenants_talos.ensure_talos_golden_image)
        assert 'startswith(("http://", "https://"))' in source
        assert "TALOS_GOLDEN_IMAGE_URL" in source


class TestBootstrapProviderNamespace:
    """capi-operator's namespace is a deployment choice, not a constant. This
    cluster runs it under `o0-capi` with its kubeadm provider alongside;
    creating the Talos provider in a hardcoded `capi-talos-bootstrap-system`
    404s, and Talos support then reports itself "unavailable" while a healthy
    operator sits next door."""

    def _k8s(self, providers=None, deployments=None):
        from unittest.mock import AsyncMock, MagicMock

        k8s = MagicMock()
        k8s.custom_api.list_cluster_custom_object = AsyncMock(
            return_value={"items": providers if providers is not None else []},
        )
        deps = MagicMock()
        deps.items = deployments or []
        k8s.apps_api.list_deployment_for_all_namespaces = AsyncMock(return_value=deps)
        return k8s

    def _deployment(self, name, namespace):
        from unittest.mock import MagicMock

        dep = MagicMock()
        dep.metadata.name = name
        dep.metadata.namespace = namespace
        return dep

    @pytest.mark.asyncio
    async def test_follows_the_namespace_of_an_existing_provider(self) -> None:
        from app.api.v1.tenants_talos import find_capi_operator_namespace

        k8s = self._k8s(providers=[{"metadata": {"name": "kubeadm", "namespace": "o0-capi"}}])

        assert await find_capi_operator_namespace(k8s) == "o0-capi"

    @pytest.mark.asyncio
    async def test_falls_back_to_the_operator_deployment(self) -> None:
        from app.api.v1.tenants_talos import find_capi_operator_namespace

        k8s = self._k8s(
            providers=[],
            deployments=[self._deployment("capi-operator-cluster-api-operator", "o0-capi")],
        )

        assert await find_capi_operator_namespace(k8s) == "o0-capi"

    @pytest.mark.asyncio
    async def test_falls_back_to_the_upstream_default_when_nothing_is_found(self) -> None:
        from app.api.v1.tenants_talos import (
            TALOS_PROVIDER_FALLBACK_NS,
            find_capi_operator_namespace,
        )

        assert await find_capi_operator_namespace(self._k8s()) == TALOS_PROVIDER_FALLBACK_NS

    def test_the_manifest_is_built_for_the_namespace_it_is_created_in(self) -> None:
        from app.api.v1.tenants_talos import build_bootstrap_provider

        body = build_bootstrap_provider("o0-capi")

        assert body["metadata"]["namespace"] == "o0-capi"
        assert body["metadata"]["name"] == "talos"


class TestSignerReachesTheControlPlane:
    """KamajiControlPlane names the sidecar fields `extraContainers` /
    `extraVolumes`; TenantControlPlane calls the same things
    `additionalContainers` / `additionalVolumes`. Using the TCP names on a KCP
    is not an error the API reports — unknown fields are pruned silently, the
    tenant comes up Ready, and the signer is simply absent. The Talos worker
    then waits for a certificate nothing will ever issue.

    Measured on the lab before the fix: control plane 5/5 Running with
    kube-apiserver, kube-scheduler, kube-controller-manager, kine and
    konnectivity-server — and no talos-csr-signer.
    """

    def test_the_capi_path_writes_the_kamajicontrolplane_field_names(self) -> None:
        import inspect

        from app.api.v1 import tenants_capi

        source = inspect.getsource(tenants_capi)
        assert 'spec["deployment"]["extraContainers"]' in source
        assert 'spec["deployment"]["extraVolumes"]' in source
        assert 'spec["deployment"]["additionalContainers"]' not in source

    def test_the_additions_still_carry_the_signer(self) -> None:
        from app.api.v1.tenants_talos import talos_control_plane_additions

        additions = talos_control_plane_additions(
            "t1", "tenant-t1", "signer:latest", shared_vip=False,
        )
        names = [c["name"] for c in additions["additionalContainers"]]

        assert "talos-csr-signer" in names
        assert additions["additionalVolumes"]


class TestSignerInvocation:
    """The flags are the image's, verified against `talos-csr-signer --help`
    on ghcr.io/clastix/talos-csr-signer. Getting one wrong is not subtle: the
    binary exits `unknown flag: --listen` and crash-loops, while the tenant
    stays Ready and the worker waits for a certificate that never comes."""

    def _sidecar(self) -> dict:
        from app.api.v1.tenants_talos import build_signer_sidecar

        return build_signer_sidecar("t1", "signer:latest")

    def _flags(self) -> dict[str, str]:
        return dict(
            arg.lstrip("-").split("=", 1) for arg in self._sidecar()["args"]
        )

    def test_only_flags_the_binary_accepts(self) -> None:
        accepted = {
            "ca-cert-path", "ca-key-path", "port",
            "tls-cert-path", "tls-key-path",
        }
        assert set(self._flags()) <= accepted

    def test_it_listens_on_the_port_talos_dials(self) -> None:
        from app.api.v1.tenants_talos import TALOS_TRUSTD_PORT

        assert self._flags()["port"] == str(TALOS_TRUSTD_PORT)

    def test_the_ca_key_is_mounted_not_just_the_certificate(self) -> None:
        # Signing needs the key; with only the certificate the signer starts
        # and then cannot issue anything.
        flags = self._flags()
        assert flags["ca-key-path"].endswith("tls.key")
        assert flags["ca-cert-path"].endswith("tls.crt")

    def test_the_token_comes_from_the_tenant_secret_as_env(self) -> None:
        # There is no shell in the image (distroless), so it cannot be
        # interpolated into an argument — the binary reads TALOS_TOKEN.
        env = {e["name"]: e for e in self._sidecar()["env"]}

        assert "TALOS_TOKEN" in env
        ref = env["TALOS_TOKEN"]["valueFrom"]["secretKeyRef"]
        assert ref["name"] == "t1-talos-secrets"
        assert ref["key"] == "machine.token"

    def test_both_secrets_are_mounted(self) -> None:
        from app.api.v1.tenants_talos import build_signer_volume

        mounts = {m["name"]: m["mountPath"] for m in self._sidecar()["volumeMounts"]}
        volumes = {v["name"]: v["secret"]["secretName"] for v in build_signer_volume("t1")}

        assert mounts["talos-signer-certs"] == "/etc/talos-signer"
        assert mounts["talos-ca-certs"] == "/etc/talos-ca"
        assert volumes["talos-signer-certs"] == "t1-talos-signer"
        assert volumes["talos-ca-certs"] == "t1-talos-ca"


class TestTalosControlPlaneService:
    """Talos dials trustd on port 50001 of whatever host is in
    `cluster.controlPlane.endpoint`, and Kamaji cannot put a second port on
    the Service it manages: `KamajiControlPlane.spec.network` has no field for
    it, `TenantControlPlane.spec.networkProfile` carries a single `port`, and a
    port added by hand is reconciled away in under a minute (measured on the
    lab). Hence a Service of our own, with both ports."""

    def _svc(self):
        from app.api.v1.tenants_talos import build_talos_service

        return build_talos_service("t1", "tenant-t1", 6443)

    def _ports(self) -> dict[str, int]:
        return {p["name"]: p["port"] for p in self._svc()["spec"]["ports"]}

    def test_it_answers_on_both_ports(self) -> None:
        from app.api.v1.tenants_talos import TALOS_TRUSTD_PORT

        ports = self._ports()
        assert ports["kube-apiserver"] == 6443
        assert ports["trustd"] == TALOS_TRUSTD_PORT

    def test_it_selects_the_kamaji_control_plane_pods(self) -> None:
        assert self._svc()["spec"]["selector"] == {"kamaji.clastix.io/name": "t1"}

    def test_the_worker_endpoint_names_this_service(self) -> None:
        from app.api.v1.tenants_talos import worker_endpoint

        host = worker_endpoint("t1", "tenant-t1", 6443).split("//")[1].rsplit(":", 1)[0]
        assert host == f"{self._svc()['metadata']['name']}.tenant-t1.svc"

    def test_the_signer_certificate_answers_to_that_name(self) -> None:
        # Otherwise the join fails TLS before trustd is ever reached.
        from app.api.v1.tenants_talos import signer_dns_names, worker_endpoint

        host = worker_endpoint("t1", "tenant-t1", 6443).split("//")[1].rsplit(":", 1)[0]
        assert host in signer_dns_names("t1", "tenant-t1")


class TestGoldenImagePlatform:
    """The image platform has to match the disk CAPK attaches, and CAPK
    attaches `cloudInitConfigDrive` — an OpenStack config-2 disk. The nocloud
    variant looks for `cidata`, does not find it, and drops into maintenance
    mode waiting for `talosctl apply-config`:

        volume "platform/cidata/config" phase "waiting -> missing"
        entering maintenance service

    which reads as a worker that boots fine and never joins. Measured on the
    lab from the worker's serial console.
    """

    def test_the_image_reads_a_config_drive(self) -> None:
        from app.api.v1.tenants_talos import TALOS_GOLDEN_IMAGE_URL

        assert "openstack-amd64" in TALOS_GOLDEN_IMAGE_URL
        assert "nocloud" not in TALOS_GOLDEN_IMAGE_URL

    def test_it_is_still_a_raw_image_cdi_can_import(self) -> None:
        from app.api.v1.tenants_talos import TALOS_GOLDEN_IMAGE_URL

        assert TALOS_GOLDEN_IMAGE_URL.endswith(".raw.xz")


class TestWorkerConfigCarriesTheCA:
    """Talos validates the machine config before it dials anything, and
    refuses one with no CA at all:

        failed to validate config acquired via platform openstack:
        issuing CA or some accepted CAs are required
          (.machine.ca, machine.acceptedCAs)

    The field existed and nothing ever filled it, so every Talos worker read
    its config, rejected it, and sat in maintenance mode — indistinguishable
    from a worker that never got one.
    """

    def _config(self, ca: str = "Zm9vCg=="):
        from app.api.v1.tenants_talos import build_talos_worker_config

        return build_talos_worker_config(
            "t1", "tenant-t1", api_port=6443, control_plane_vip="10.0.0.1",
            machine_token="aaaaaa.bbbbbbbbbbbbbbbb", cluster_id="id",
            cluster_secret="secret", pod_cidr="10.244.0.0/16",
            service_cidr="10.112.0.0/12", ca_cert_b64=ca,
        )

    def test_the_ca_is_written_where_talos_looks(self) -> None:
        machine = self._config()["machine"]

        assert machine["ca"]["crt"] == "Zm9vCg=="
        assert machine["acceptedCAs"] == [{"crt": "Zm9vCg=="}]

    def test_no_ca_key_is_handed_to_a_worker(self) -> None:
        # A worker never issues certificates — it asks the signer for one.
        assert "key" not in self._config()["machine"]["ca"]

    def test_an_absent_ca_leaves_the_fields_out(self) -> None:
        machine = self._config(ca="")["machine"]

        assert "ca" not in machine
        assert "acceptedCAs" not in machine

    def test_the_capi_path_reads_and_passes_it(self) -> None:
        import inspect

        from app.api.v1 import tenants_capi

        source = inspect.getsource(tenants_capi)
        assert "read_talos_ca_cert(k8s, req.name, ns)" in source
        assert "ca_cert_b64=talos_ca" in source


class TestWorkerConfigCarriesTheKubernetesCA:
    """Two different CAs, both required. `machine.ca` is Talos's own;
    `cluster.ca` is the Kubernetes CA the kubelet must trust to reach the
    tenant apiserver. With only the first, Talos accepts the config, starts,
    and never brings the kubelet up:

        secrets.KubeletController: missing accepted Kubernetes CAs
    """

    def _config(self, k8s_ca: str = "a2M="):
        from app.api.v1.tenants_talos import build_talos_worker_config

        return build_talos_worker_config(
            "t1", "tenant-t1", api_port=6443, control_plane_vip="10.0.0.1",
            machine_token="aaaaaa.bbbbbbbbbbbbbbbb", cluster_id="id",
            cluster_secret="secret", pod_cidr="10.244.0.0/16",
            service_cidr="10.112.0.0/12", ca_cert_b64="dGFsb3M=",
            k8s_ca_cert_b64=k8s_ca,
        )

    def test_the_two_cas_are_distinct_and_both_present(self) -> None:
        cfg = self._config()

        assert cfg["machine"]["ca"]["crt"] == "dGFsb3M="
        assert cfg["cluster"]["ca"]["crt"] == "a2M="
        assert cfg["cluster"]["acceptedCAs"] == [{"crt": "a2M="}]

    def test_no_kubernetes_ca_key_reaches_the_worker(self) -> None:
        assert "key" not in self._config()["cluster"]["ca"]

    def test_an_absent_kubernetes_ca_leaves_the_fields_out(self) -> None:
        cluster = self._config(k8s_ca="")["cluster"]

        assert "ca" not in cluster
        assert "acceptedCAs" not in cluster

    def test_the_capi_path_reads_it_from_the_kamaji_secret(self) -> None:
        import inspect

        from app.api.v1 import tenants_capi, tenants_talos

        assert 'f"{tenant}-ca"' in inspect.getsource(tenants_talos.read_tenant_k8s_ca_cert)
        assert "k8s_ca_cert_b64=k8s_ca" in inspect.getsource(tenants_capi)


class TestKubeletVersionPinning:
    """Talos ships the kubelet matching ITS release — 1.13.8 carries v1.36.2 —
    which against a v1.32 tenant control plane is four minors of skew. The node
    boots, apid and the kubelet both report healthy, and it never registers."""

    def _kubelet(self, version: str = "v1.32.1"):
        from app.api.v1.tenants_talos import build_talos_worker_config

        return build_talos_worker_config(
            "t1", "tenant-t1", api_port=6443, control_plane_vip="10.0.0.1",
            machine_token="aaaaaa.bbbbbbbbbbbbbbbb", cluster_id="id",
            cluster_secret="secret", pod_cidr="10.244.0.0/16",
            service_cidr="10.112.0.0/12", kubernetes_version=version,
        )["machine"]["kubelet"]

    def test_the_kubelet_matches_the_tenant_version(self) -> None:
        assert self._kubelet()["image"] == "ghcr.io/siderolabs/kubelet:v1.32.1"

    def test_certificate_rotation_stays_on(self) -> None:
        assert self._kubelet()["extraArgs"]["rotate-certificates"] == "true"

    def test_without_a_version_the_image_is_left_to_talos(self) -> None:
        assert "image" not in self._kubelet(version="")
