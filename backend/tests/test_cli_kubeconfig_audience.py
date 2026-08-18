"""The kubeconfig from CLI Access authenticated as nobody.

Reported from a real download: the file connected, TLS verified, and the
apiserver answered

    the server has asked for the client to provide credentials

with a token that was neither expired nor malformed. It was simply addressed
to somebody else. The token carried

    aud: ["https://10.96.0.1:443"]      the in-cluster apiserver address
    iss: "https://10.198.175.250:6443"  the issuer, i.e. the external one

and an apiserver validates the audience against its own `--api-audiences`,
which defaults to the issuer. Measured on the cluster:

    audience https://10.96.0.1:443  ->  401
    no audience requested           ->  200

The code asked for an audience it had guessed, and a comment beside it stated
the guess as fact — "the apiserver validates tokens against its own
self-identity, not the external URL". That comment is why the bug survived
review: it explained the wrong behaviour convincingly.

Requesting no audience removes the guess. The apiserver stamps its own
default, which is the one it accepts by definition, and there is nothing left
for a deployment to keep in step.
"""

import inspect

from app.api.v1 import auth


class TestTheTokenRequestAsksForNoAudience:
    def test_no_url_is_passed_as_an_audience(self) -> None:
        src = inspect.getsource(auth._ensure_service_account)

        assert "audiences=[]" in src, (
            "an audience is being requested again — it can only be a guess at "
            "the apiserver's --api-audiences"
        )
        assert "audiences=[api_server_url]" not in src

    def test_the_empty_list_is_deliberate_and_explained(self) -> None:
        """`None` is rejected by the client model, so the empty list is the way
        to say "your default" — worth a sentence, since it looks like an
        oversight."""
        src = inspect.getsource(auth._ensure_service_account)

        assert "must not be `None`" in src or "default" in src

    def test_the_function_no_longer_takes_an_api_server_url(self) -> None:
        """The parameter existed only to be used as the audience. Leaving it in
        the signature would invite the same guess back."""
        params = inspect.signature(auth._ensure_service_account).parameters

        assert "api_server_url" not in params


class TestTheKubeconfigStillPointsAtTheExternalAddress:
    def test_the_server_comes_from_discovery_not_from_the_audience(self) -> None:
        """These are two different addresses and always were: the file has to
        reach the apiserver from a laptop, while the token has to satisfy the
        apiserver's own idea of who it is. Conflating them is what produced a
        file that resolved, connected, and authenticated as nobody."""
        src = inspect.getsource(auth.get_kubeconfig)

        assert "discover_external_api_url" in src
        assert "internal_server" in src
