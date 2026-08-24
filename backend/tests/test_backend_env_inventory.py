"""Every knob the backend reads has to be declared where operators look.

A variable nobody knows about is a feature that silently does nothing. B3
routed egress shipped disabled on an entire stand for exactly that reason:
`deploy.sh` never set B3_BGP_PEER, nothing in the chart mentioned it, the
backend read it, got nothing, and generated no announcements — no error, no
warning, no clue. Tenants simply had no egress (UAT 2026-08-19, finding A4).

The chart's `backend.env` is that place. Keys are declared with empty values
and skipped at render time, so declaring one costs nothing and changes no
output — it only makes the knob findable.

This test is the thing that keeps the list honest. A new `os.getenv` without
a matching key fails here, in the commit that adds it, rather than as a
feature that quietly does nothing on someone else's cluster.
"""

import re
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parents[1] / "app"
_CANDIDATES = [
    Path("/helm") / "kubevirt-ui" / "values.yaml",
    Path(__file__).resolve().parents[2] / "helm" / "kubevirt-ui" / "values.yaml",
]
VALUES = next((p for p in _CANDIDATES if p.is_file()), None)

pytestmark = pytest.mark.skipif(
    VALUES is None,
    reason=(
        "Helm chart not reachable; mount it at /helm to run the env inventory "
        f"test (looked in: {', '.join(str(p) for p in _CANDIDATES)})"
    ),
)

# Product configuration, as opposed to plumbing like LOG_LEVEL: these are the
# prefixes whose absence changes what the product does rather than how loudly
# it says it.
CONFIG_PREFIXES = ("B3_", "TENANTS_", "OPERATOR_")


def _env_read_by_the_backend() -> set[str]:
    """Every configuration variable name the backend mentions.

    Deliberately not `os.getenv("...")`: two variables are read through a
    constant (`CATALOG_ENV = "TENANTS_TALOS_CATALOG"`), and a scan tied to the
    call site missed both — which is how one of them stayed undeclared while
    a test claimed the list was complete. Matching the literal wherever it
    appears cannot be dodged by moving the read one line away.
    """
    pattern = re.compile(r'"((?:B3_|TENANTS_|OPERATOR_)[A-Z0-9_]+)"')
    return {
        m.group(1)
        for path in _APP.rglob("*.py")
        for m in pattern.finditer(path.read_text())
    }


def _env_declared_by_the_chart() -> set[str]:
    # Keys under backend.env, which sit at exactly four spaces of indent.
    return {
        m.group(1)
        for m in re.finditer(r"^    ([A-Z0-9_]+):", VALUES.read_text(), re.M)
        if m.group(1).startswith(CONFIG_PREFIXES)
    }


def test_every_variable_the_backend_reads_is_declared() -> None:
    missing = sorted(_env_read_by_the_backend() - _env_declared_by_the_chart())

    assert not missing, (
        "the backend reads these and the chart never mentions them, so nobody "
        "setting up a stand can discover they exist — declare each with an "
        f"empty value under backend.env in values.yaml: {missing}"
    )


def test_the_chart_declares_nothing_the_backend_stopped_reading() -> None:
    stale = sorted(_env_declared_by_the_chart() - _env_read_by_the_backend())

    assert not stale, (
        "these are declared in the chart but nothing reads them any more — a "
        "knob that looks configurable and is not is worse than an absent one: "
        f"{stale}"
    )


def test_the_inventory_is_not_trivially_empty() -> None:
    # Guards the two above: if the scraping ever stops matching, both would
    # pass on empty sets while checking nothing at all.
    assert len(_env_read_by_the_backend()) > 20
