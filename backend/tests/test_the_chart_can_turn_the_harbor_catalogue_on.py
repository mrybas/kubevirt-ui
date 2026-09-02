"""The chart has to be able to enable what the backend implements.

`grep -ri harbor helm/` returned nothing while the whole Harbor catalogue —
list, materialise, publish — sat behind `HARBOR_IMAGE_ENABLED`. The feature
was therefore unreachable through the only supported way this product is
installed: you had to edit the Deployment by hand, and if you did, you also
had to know that HARBOR_URL exists, because without it materialise answers
503 and the registry host every reference is built from is empty.

This is the same class as `test_helm_rbac_contract.py`: correct Python,
passing tests, and a feature that cannot be switched on where it runs.
"""

import re
from pathlib import Path

import pytest
import yaml

_CHART = Path("kubevirt-ui")
# In the test container only ./backend is mounted at /app, so the chart comes
# in separately at /helm (see docker-compose.yml). Outside it, walk up.
_CANDIDATES = [Path("/helm") / _CHART, Path(__file__).resolve().parents[2] / "helm" / _CHART]
CHART_DIR = next((p for p in _CANDIDATES if (p / "values.yaml").is_file()), None)

pytestmark = pytest.mark.skipif(
    CHART_DIR is None,
    reason=(
        "Helm chart not reachable; mount it at /helm to run the chart contract "
        f"test (looked in: {', '.join(str(p) for p in _CANDIDATES)})"
    ),
)

# The settings that decide whether the feature runs and where it points.
# Deliberately not every HARBOR_* the code reads: HARBOR_TIMEOUT_SECONDS and
# HARBOR_FETCH_CONCURRENCY are tuning knobs with working defaults, and
# `backend.env` passes any key through anyway. These four are the ones an
# operator cannot guess.
REQUIRED_KEYS = [
    "HARBOR_IMAGE_ENABLED",
    "HARBOR_URL",
    "HARBOR_ROBOT_SECRET",
    "HARBOR_CA_CONFIGMAP",
]


def _backend_env() -> dict:
    values = yaml.safe_load((CHART_DIR / "values.yaml").read_text())
    return values["backend"]["env"]


@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_the_setting_exists_in_values(key: str) -> None:
    assert key in _backend_env(), (
        f"{key} is read by the backend and absent from the chart, so no "
        "supported install can set it"
    )


@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_the_setting_defaults_to_off(key: str) -> None:
    """Empty string, like every OPERATOR_*_ENABLED sibling.

    The deployment template skips empty values, so an unset key emits no env
    var at all and the backend keeps its own default — which for
    HARBOR_IMAGE_ENABLED is off. A chart that shipped this on by default
    would start calling a Harbor that most installs do not have.
    """
    assert _backend_env()[key] == "", f"{key} must default to off/unset"


def test_a_non_empty_value_actually_reaches_the_container() -> None:
    """values.yaml alone proves nothing — the template has to emit it.

    `backend.env` is passed through by a generic range that skips empty
    values; this pins that the loop is still there, since without it the four
    keys above would be documentation for a switch that is not wired to
    anything.
    """
    template = (CHART_DIR / "templates" / "backend-deployment.yaml").read_text()
    assert "range $key, $value := .Values.backend.env" in template
    assert re.search(r"- name: \{\{ \$key \}\}\s*\n\s*value: \{\{ \$value \| quote \}\}", template)


@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_the_setting_is_documented(key: str) -> None:
    readme = (CHART_DIR / "README.md").read_text()
    assert f"backend.env.{key}" in readme, f"{key} is not in the chart's README table"


def test_the_flag_the_chart_names_is_the_flag_the_code_reads() -> None:
    """Two spellings of one name is the failure this catches.

    A chart key that no code reads is silent: the operator sets it, nothing
    happens, and there is nothing to grep for.
    """
    from app.core import operator

    source = Path(operator.__file__).read_text()
    assert '_enabled("HARBOR_IMAGE_ENABLED")' in source


def test_the_url_the_chart_names_is_the_url_the_code_reads() -> None:
    from app.core import harbor_client

    source = Path(harbor_client.__file__).read_text()
    for key in ("HARBOR_URL", "HARBOR_ROBOT_SECRET", "HARBOR_CA_CONFIGMAP"):
        assert f'os.getenv("{key}"' in source, f"{key} is not read anywhere"
