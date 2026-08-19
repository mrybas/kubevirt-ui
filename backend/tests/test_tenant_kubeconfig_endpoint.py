"""The kubeconfig has to name an address the person downloading it can reach.

Kamaji writes its Secret against the in-cluster address — on this stand the
private control-plane VIP, `https://10.199.0.100:6443`, which resolves
nowhere useful from a workstation. The route already swaps it for the ingress
hostname, and that hostname is in the apiserver's `certSANs`, so the result
verifies rather than needing `insecure-skip-tls-verify`.

Nothing guarded that. It is exactly the shape of change that survives review
and fails on someone else's laptop weeks later, because the person who wrote
it tested from inside the cluster.

(Recorded during UAT as finding F-5, "the product hands out the internal
VIP". That was wrong: the kubeconfig measured came from `kubectl get secret`,
which is Kamaji's raw output, not from this endpoint. Retracted — the gap was
never the behaviour, only that nothing held it in place.)
"""

import base64

import pytest
import yaml

from app.api.v1.tenants_crud import _kubeconfig_with_external_server

INTERNAL = """
apiVersion: v1
kind: Config
clusters:
  - name: uat-t1
    cluster:
      server: https://10.199.0.100:6443
      certificate-authority-data: QUJD
users:
  - name: admin
    user:
      client-certificate-data: REVG
"""


def _server_of(kubeconfig: str) -> str:
    return yaml.safe_load(kubeconfig)["clusters"][0]["cluster"]["server"]


def test_the_internal_address_never_reaches_the_operator() -> None:
    out = _kubeconfig_with_external_server(
        INTERNAL, "uat-t1.tenants.lab.beardlabs.cc",
    )

    assert _server_of(out) == "https://uat-t1.tenants.lab.beardlabs.cc"
    assert "10.199.0.100" not in out, (
        "the private VIP is still in the file the operator downloads"
    )


def test_the_credentials_are_left_alone() -> None:
    # Only the address is wrong in Kamaji's output. Touching the CA is how a
    # swap like this turns into `insecure-skip-tls-verify` by accident.
    out = yaml.safe_load(
        _kubeconfig_with_external_server(INTERNAL, "t1.example.com"),
    )

    assert out["clusters"][0]["cluster"]["certificate-authority-data"] == "QUJD"
    assert out["users"][0]["user"]["client-certificate-data"] == "REVG"
    assert "insecure-skip-tls-verify" not in yaml.dump(out)


def test_every_cluster_entry_is_rewritten() -> None:
    two = yaml.safe_load(INTERNAL)
    two["clusters"].append(
        {"name": "second", "cluster": {"server": "https://10.199.0.101:6443"}},
    )

    out = _kubeconfig_with_external_server(yaml.dump(two), "t1.example.com")

    servers = {
        c["cluster"]["server"] for c in yaml.safe_load(out)["clusters"]
    }
    assert servers == {"https://t1.example.com"}


def test_the_host_is_the_one_the_certificate_covers() -> None:
    """The swap is only safe because certSANs carries the same hostname.

    `_endpoint_host` builds it and the TenantControlPlane is created with it
    in `network.certSANs`; if those two ever diverge the kubeconfig starts
    failing TLS instead of connecting, which is worse than the internal
    address it replaced.
    """
    from pathlib import Path

    capi = (
        Path(__file__).resolve().parents[1]
        / "app" / "api" / "v1" / "tenants_capi.py"
    ).read_text()

    assert '"certSANs": [_endpoint_host(req.name)]' in capi, (
        "the apiserver certificate no longer covers the host the kubeconfig "
        "points at — the download will fail TLS verification"
    )
