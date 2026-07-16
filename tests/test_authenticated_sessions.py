"""Unit tests for :mod:`O4_Authenticated_Sessions` (headless, no network).

Every test redirects the module's writable ``Sessions`` directory into a
``tmp_path`` subdirectory (so cookie files and username sidecars never
touch the real data root), and injects a fake ``keyring`` module for the
credential-store paths.  Only the cookie save/load round-trip exercises
the genuine :func:`build_session` / :func:`save_session_cookies` on a real
``requests.Session`` -- no request is ever issued.
"""

import http.cookiejar
import json
import os
import stat
import sys
import types
from typing import Callable, Dict, List, Optional, Tuple

import pytest

import O4_Authenticated_Sessions as SESSIONS


# =====================================================================
# Shared fixtures and fakes
# =====================================================================
@pytest.fixture
def sessions_directory(monkeypatch, tmp_path) -> str:
    """Redirect the module's session directory into ``tmp_path``.

    :func:`cookie_file_path` and the username sidecar both derive from
    :func:`sessions_directory`, so patching this one function relocates
    every session-related write.
    """
    directory = str(tmp_path / "Sessions")
    monkeypatch.setattr(SESSIONS, "sessions_directory", lambda: directory)
    return directory


def _install_fake_keyring(
    monkeypatch, backend_module: str = "tests.fake_backend"
) -> Dict[Tuple[str, str], str]:
    """Insert a fake ``keyring`` (and ``keyring.errors``) into ``sys.modules``.

    Returns the backing dictionary keyed by ``(service, username)`` so a
    test can inspect or mutate stored secrets directly.  ``backend_module``
    sets the reported ``__module__`` of the backend object; pass
    ``"keyring.backends.fail"`` to model a machine with no usable store.
    """
    store: Dict[Tuple[str, str], str] = {}
    fake_keyring = types.ModuleType("keyring")
    fake_errors = types.ModuleType("keyring.errors")

    class KeyringError(Exception):
        """Stand-in for keyring.errors.KeyringError."""

    fake_errors.KeyringError = KeyringError

    class _Backend:
        """Fake secret-store backend object."""

    _Backend.__module__ = backend_module

    def get_keyring() -> object:
        return _Backend()

    def set_password(service: str, username: str, password: str) -> None:
        store[(service, username)] = password

    def get_password(service: str, username: str) -> Optional[str]:
        return store.get((service, username))

    def delete_password(service: str, username: str) -> None:
        store.pop((service, username), None)

    fake_keyring.get_keyring = get_keyring
    fake_keyring.set_password = set_password
    fake_keyring.get_password = get_password
    fake_keyring.delete_password = delete_password
    fake_keyring.errors = fake_errors

    monkeypatch.setitem(sys.modules, "keyring", fake_keyring)
    monkeypatch.setitem(sys.modules, "keyring.errors", fake_errors)
    return store


class FakeResponse:
    """Minimal stand-in for a ``requests`` response."""

    def __init__(
        self,
        status_code: int = 200,
        headers: Optional[Dict[str, str]] = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self.headers = headers if headers is not None else {}
        self.text = text
        self.cookies: List[object] = []


class FakeSession:
    """Record-and-reply stand-in for a ``requests.Session``.

    ``get_handler`` / ``post_handler`` are callables ``(url, **kwargs) ->
    FakeResponse`` (raising to simulate a network failure).  ``cookies`` is
    a plain list so :meth:`clear` and iteration behave like a cookie jar
    for the code paths under test.
    """

    def __init__(
        self,
        get_handler: Optional[Callable[..., FakeResponse]] = None,
        post_handler: Optional[Callable[..., FakeResponse]] = None,
    ) -> None:
        self._get_handler = get_handler
        self._post_handler = post_handler
        self.get_calls: List[Tuple[str, dict]] = []
        self.post_calls: List[Tuple[str, dict]] = []
        self.cookies: List[object] = []

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.get_calls.append((url, kwargs))
        if self._get_handler is None:
            return FakeResponse(200)
        return self._get_handler(url, **kwargs)

    def post(self, url: str, **kwargs) -> FakeResponse:
        self.post_calls.append((url, kwargs))
        if self._post_handler is None:
            return FakeResponse(200)
        return self._post_handler(url, **kwargs)


def _definition() -> dict:
    """A representative signed-in provider definition."""
    return {
        "code": "PORTUGAL50CM",
        "session_name": "dgterritorio",
        "login_flow": "keycloak_password",
        "login_url": "https://example.invalid/auth/login",
        "session_probe_url": "https://example.invalid/backend/?f=json",
    }


# =====================================================================
# 1. Cookie save / load round-trip (real requests.Session, no network)
# =====================================================================
def _make_cookie(
    name: str, value: str, expires: Optional[int], discard: bool
) -> http.cookiejar.Cookie:
    """Build a standard-library cookie for the persistence round-trip."""
    return http.cookiejar.Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain="example.invalid",
        domain_specified=True,
        domain_initial_dot=False,
        path="/",
        path_specified=True,
        secure=False,
        expires=expires,
        discard=discard,
        comment=None,
        comment_url=None,
        rest={},
        rfc2109=False,
    )


def test_cookie_round_trip_preserves_session_cookies(sessions_directory):
    """Persisted cookies -- including a discard-only session cookie -- reload.

    A browser session cookie (``expires=None``, ``discard=True``) is
    exactly what the login flow leaves behind; the module saves and loads
    with ``ignore_discard=True`` so it must survive a save + fresh build.
    """
    session_name = "dgterritorio"
    session = SESSIONS.build_session(session_name)
    session.cookies.set_cookie(
        _make_cookie("persistent", "keep", 4102444800, False)
    )
    session.cookies.set_cookie(
        _make_cookie("session_only", "ephemeral", None, True)
    )

    SESSIONS.save_session_cookies(session, session_name)

    reloaded = SESSIONS.build_session(session_name)
    names = {cookie.name: cookie.value for cookie in reloaded.cookies}
    assert names["persistent"] == "keep"
    assert names["session_only"] == "ephemeral"


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX permission bits only"
)
def test_cookie_file_and_directory_owner_only(sessions_directory):
    """Cookie file gets 0o600 and its directory 0o700 on POSIX."""
    session_name = "dgterritorio"
    session = SESSIONS.build_session(session_name)
    session.cookies.set_cookie(
        _make_cookie("persistent", "keep", 4102444800, False)
    )
    SESSIONS.save_session_cookies(session, session_name)

    path = SESSIONS.cookie_file_path(session_name)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(sessions_directory).st_mode) == 0o700


# =====================================================================
# 2. Credential store
# =====================================================================
def test_store_load_delete_credentials_happy_path(
    sessions_directory, monkeypatch
):
    """Store then load returns the pair; delete then load returns None."""
    _install_fake_keyring(monkeypatch)
    SESSIONS.store_credentials("dgterritorio", "pilot", "secret-pass")

    assert SESSIONS.load_credentials("dgterritorio") == (
        "pilot",
        "secret-pass",
    )

    SESSIONS.delete_credentials("dgterritorio")
    assert SESSIONS.load_credentials("dgterritorio") is None


def test_store_credentials_raises_without_backend(
    sessions_directory, monkeypatch
):
    """A ``keyring.backends.fail`` backend is treated as no store at all."""
    _install_fake_keyring(monkeypatch, backend_module="keyring.backends.fail")
    assert SESSIONS.credential_store_available() is False
    with pytest.raises(SESSIONS.CredentialStoreUnavailable):
        SESSIONS.store_credentials("dgterritorio", "pilot", "secret-pass")


def test_load_credentials_none_when_sidecar_missing(
    sessions_directory, monkeypatch
):
    """No username sidecar => None (never touches the store)."""
    _install_fake_keyring(monkeypatch)
    assert SESSIONS.load_credentials("dgterritorio") is None


def test_load_credentials_none_when_password_missing(
    sessions_directory, monkeypatch
):
    """Sidecar present but the store has no password => None."""
    store = _install_fake_keyring(monkeypatch)
    SESSIONS.store_credentials("dgterritorio", "pilot", "secret-pass")
    store.clear()  # username sidecar remains; secret is gone
    assert SESSIONS.load_credentials("dgterritorio") is None


def test_load_credentials_none_when_keyring_import_fails(
    sessions_directory, monkeypatch, capsys
):
    """A ``keyring`` import that explodes is swallowed => None + warning."""
    _install_fake_keyring(monkeypatch)
    SESSIONS.store_credentials("dgterritorio", "pilot", "secret-pass")
    # Now make ``import keyring`` raise ImportError inside load_credentials.
    monkeypatch.setitem(sys.modules, "keyring", None)

    assert SESSIONS.load_credentials("dgterritorio") is None
    assert "WARNING" in capsys.readouterr().out


# =====================================================================
# 3. probe_signed_in
# =====================================================================
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 401, 403])
def test_probe_signed_out_statuses(status):
    """Redirect and 401/403 responses report signed out."""
    session = FakeSession(get_handler=lambda url, **kw: FakeResponse(status))
    assert SESSIONS.probe_signed_in(session, _definition()) is False


@pytest.mark.parametrize("status", [200, 404])
def test_probe_signed_in_statuses(status):
    """200 and even 404 (backend router miss) report signed in."""
    session = FakeSession(get_handler=lambda url, **kw: FakeResponse(status))
    assert SESSIONS.probe_signed_in(session, _definition()) is True


def test_probe_network_error_reports_signed_out():
    """A raised request counts as signed out (re-login rather than trust)."""

    def _boom(url, **kwargs):
        raise ConnectionError("network down")

    session = FakeSession(get_handler=_boom)
    assert SESSIONS.probe_signed_in(session, _definition()) is False


def test_probe_missing_probe_url_reports_signed_out():
    """No ``session_probe_url`` => signed out, no request attempted."""
    session = FakeSession()
    assert SESSIONS.probe_signed_in(session, {}) is False
    assert session.get_calls == []


# =====================================================================
# 4. ensure_session
# =====================================================================
def _sequenced_get(statuses: List[int]) -> Callable[..., FakeResponse]:
    """A get handler returning ``statuses`` in order (last value repeats)."""
    box = {"index": 0}

    def _handler(url, **kwargs) -> FakeResponse:
        index = min(box["index"], len(statuses) - 1)
        box["index"] += 1
        return FakeResponse(statuses[index])

    return _handler


def test_ensure_session_short_circuits_when_signed_in(
    sessions_directory, monkeypatch
):
    """A valid persisted session returns immediately; no flow, no save."""
    session = FakeSession(get_handler=lambda url, **kw: FakeResponse(200))
    monkeypatch.setattr(SESSIONS, "build_session", lambda name: session)
    flow_calls: List[tuple] = []
    monkeypatch.setattr(
        SESSIONS,
        "run_login_flow",
        lambda *args: flow_calls.append(args),
    )

    result = SESSIONS.ensure_session(_definition())
    assert result is session
    assert flow_calls == []
    assert not os.path.exists(
        SESSIONS.cookie_file_path("dgterritorio")
    )


def test_ensure_session_explicit_credentials_relogin_and_save(
    sessions_directory, monkeypatch
):
    """Signed out + explicit credentials => run flow, then persist cookies."""
    session = FakeSession(get_handler=_sequenced_get([302, 200]))
    monkeypatch.setattr(SESSIONS, "build_session", lambda name: session)
    flow_calls: List[tuple] = []

    def _flow(passed_session, definition, username, password):
        flow_calls.append((username, password))

    monkeypatch.setattr(SESSIONS, "run_login_flow", _flow)

    result = SESSIONS.ensure_session(
        _definition(), credentials=("pilot", "secret-pass")
    )
    assert result is session
    assert flow_calls == [("pilot", "secret-pass")]
    assert os.path.exists(SESSIONS.cookie_file_path("dgterritorio"))


def test_ensure_session_stored_credentials_auto_relogin(
    sessions_directory, monkeypatch
):
    """Signed out + no explicit credentials => use the stored account."""
    session = FakeSession(get_handler=_sequenced_get([302, 200]))
    monkeypatch.setattr(SESSIONS, "build_session", lambda name: session)
    monkeypatch.setattr(
        SESSIONS, "load_credentials", lambda name: ("stored", "stored-pass")
    )
    flow_calls: List[tuple] = []
    monkeypatch.setattr(
        SESSIONS,
        "run_login_flow",
        lambda ps, d, u, p: flow_calls.append((u, p)),
    )

    result = SESSIONS.ensure_session(_definition())
    assert result is session
    assert flow_calls == [("stored", "stored-pass")]


def test_ensure_session_no_credentials_raises_login_error(
    sessions_directory, monkeypatch
):
    """Signed out with nothing to sign in with => LoginError naming code."""
    session = FakeSession(get_handler=lambda url, **kw: FakeResponse(302))
    monkeypatch.setattr(SESSIONS, "build_session", lambda name: session)
    monkeypatch.setattr(SESSIONS, "load_credentials", lambda name: None)

    with pytest.raises(SESSIONS.LoginError) as excinfo:
        SESSIONS.ensure_session(_definition())
    message = str(excinfo.value)
    assert "PORTUGAL50CM" in message
    assert "Settings" in message


def test_ensure_session_flow_succeeds_but_probe_still_signed_out(
    sessions_directory, monkeypatch
):
    """Flow accepted but probe still fails => LoginError, no cookies saved."""
    session = FakeSession(get_handler=lambda url, **kw: FakeResponse(302))
    monkeypatch.setattr(SESSIONS, "build_session", lambda name: session)
    monkeypatch.setattr(SESSIONS, "run_login_flow", lambda *args: None)

    with pytest.raises(SESSIONS.LoginError):
        SESSIONS.ensure_session(
            _definition(), credentials=("pilot", "secret-pass")
        )
    assert not os.path.exists(SESSIONS.cookie_file_path("dgterritorio"))


# =====================================================================
# 5. sign_in
# =====================================================================
def test_sign_in_happy_path_persists_and_remembers(
    sessions_directory, monkeypatch
):
    """remember=True persists cookies and stores the credentials."""
    session = FakeSession(get_handler=lambda url, **kw: FakeResponse(200))
    monkeypatch.setattr(SESSIONS, "build_session", lambda name: session)
    monkeypatch.setattr(SESSIONS, "run_login_flow", lambda *args: None)
    stored: List[tuple] = []
    monkeypatch.setattr(
        SESSIONS,
        "store_credentials",
        lambda name, username, password: stored.append(
            (name, username, password)
        ),
    )

    result = SESSIONS.sign_in(
        _definition(), "pilot", "secret-pass", remember=True
    )
    assert result is session
    assert os.path.exists(SESSIONS.cookie_file_path("dgterritorio"))
    assert stored == [("dgterritorio", "pilot", "secret-pass")]


def test_sign_in_without_remember_does_not_store(
    sessions_directory, monkeypatch
):
    """remember=False persists cookies but never stores credentials."""
    session = FakeSession(get_handler=lambda url, **kw: FakeResponse(200))
    monkeypatch.setattr(SESSIONS, "build_session", lambda name: session)
    monkeypatch.setattr(SESSIONS, "run_login_flow", lambda *args: None)
    stored: List[tuple] = []
    monkeypatch.setattr(
        SESSIONS,
        "store_credentials",
        lambda *args: stored.append(args),
    )

    SESSIONS.sign_in(_definition(), "pilot", "secret-pass", remember=False)
    assert os.path.exists(SESSIONS.cookie_file_path("dgterritorio"))
    assert stored == []


def test_sign_in_probe_failure_raises(sessions_directory, monkeypatch):
    """A probe that still reports signed out after the flow => LoginError."""
    session = FakeSession(get_handler=lambda url, **kw: FakeResponse(302))
    monkeypatch.setattr(SESSIONS, "build_session", lambda name: session)
    monkeypatch.setattr(SESSIONS, "run_login_flow", lambda *args: None)

    with pytest.raises(SESSIONS.LoginError):
        SESSIONS.sign_in(_definition(), "pilot", "secret-pass")


# =====================================================================
# 6. sign_out
# =====================================================================
def test_sign_out_removes_cookie_file_and_credentials(
    sessions_directory, monkeypatch
):
    """sign_out deletes the cookie file, username sidecar, and secret."""
    store = _install_fake_keyring(monkeypatch)
    session = SESSIONS.build_session("dgterritorio")
    session.cookies.set_cookie(
        _make_cookie("persistent", "keep", 4102444800, False)
    )
    SESSIONS.save_session_cookies(session, "dgterritorio")
    SESSIONS.store_credentials("dgterritorio", "pilot", "secret-pass")
    assert os.path.exists(SESSIONS.cookie_file_path("dgterritorio"))

    SESSIONS.sign_out("dgterritorio")

    assert not os.path.exists(SESSIONS.cookie_file_path("dgterritorio"))
    assert SESSIONS.load_credentials("dgterritorio") is None
    assert store == {}


# =====================================================================
# 7. _find_password_form
# =====================================================================
_KEYCLOAK_LOGIN_PAGE = """
<html><body>
  <form id="kc-form-login"
        action="https://idp.example.invalid/login?tab=1&amp;code=xyz&amp;execution=e1">
    <input type="hidden" name="credentialId" value="cred-42"/>
    <input id="username" name="username" type="text" value=""/>
    <input id="password" name="password" type="password"/>
    <input type="submit" name="login" value="Sign In"/>
  </form>
</body></html>
"""


def test_find_password_form_parses_keycloak_page():
    """The action is HTML-unescaped and secret-free hidden inputs survive."""
    form = SESSIONS._find_password_form(_KEYCLOAK_LOGIN_PAGE)
    assert form is not None
    assert form["action"] == (
        "https://idp.example.invalid/login?tab=1&code=xyz&execution=e1"
    )
    assert form["inputs"]["credentialId"] == "cred-42"
    assert form["inputs"]["username"] == ""
    # The password input must NOT be echoed back into the inputs dict.
    assert "password" not in form["inputs"]


def test_find_password_form_none_without_form():
    """A page with no form yields None."""
    assert SESSIONS._find_password_form("<html><body>hi</body></html>") is None


def test_find_password_form_none_without_action():
    """A password form lacking an action URL is unusable => None."""
    page = (
        '<form id="kc-form-login">'
        '<input name="password" type="password"/></form>'
    )
    assert SESSIONS._find_password_form(page) is None


# =====================================================================
# 8. keycloak_password_login
# =====================================================================
def test_keycloak_password_login_success():
    """GET returns the form; POST returns a page without a password form."""
    post_page = "<html><body>Welcome, signed in.</body></html>"

    def _get(url, **kwargs):
        return FakeResponse(200, text=_KEYCLOAK_LOGIN_PAGE)

    def _post(url, **kwargs):
        return FakeResponse(200, text=post_page)

    session = FakeSession(get_handler=_get, post_handler=_post)
    SESSIONS.keycloak_password_login(
        session, _definition(), "pilot", "secret-pass"
    )
    # The credentials were posted to the form's unescaped action URL.
    (posted_url, posted_kwargs) = session.post_calls[0]
    assert posted_url == (
        "https://idp.example.invalid/login?tab=1&code=xyz&execution=e1"
    )
    assert posted_kwargs["data"]["username"] == "pilot"
    assert posted_kwargs["data"]["credentialId"] == "cred-42"


def test_keycloak_password_login_rejected_hides_password():
    """A re-rendered password form => LoginError without leaking the password."""

    def _get(url, **kwargs):
        return FakeResponse(200, text=_KEYCLOAK_LOGIN_PAGE)

    def _post(url, **kwargs):
        # The identity provider re-renders the login form on bad credentials.
        return FakeResponse(200, text=_KEYCLOAK_LOGIN_PAGE)

    session = FakeSession(get_handler=_get, post_handler=_post)
    with pytest.raises(SESSIONS.LoginError) as excinfo:
        SESSIONS.keycloak_password_login(
            session, _definition(), "pilot", "top-secret-value"
        )
    assert "top-secret-value" not in str(excinfo.value)


def test_keycloak_password_login_missing_login_url():
    """A definition without a ``login_url`` => LoginError before any request."""
    session = FakeSession()
    definition = _definition()
    definition.pop("login_url")
    with pytest.raises(SESSIONS.LoginError):
        SESSIONS.keycloak_password_login(
            session, definition, "pilot", "secret-pass"
        )
    assert session.get_calls == []


# =====================================================================
# 9. credential_kind
# =====================================================================
def test_credential_kind_defaults_to_session():
    """An absent or empty ``credential_kind`` => the "session" default."""
    assert SESSIONS.credential_kind({}) == SESSIONS.CREDENTIAL_KIND_SESSION
    assert (
        SESSIONS.credential_kind({"credential_kind": ""})
        == SESSIONS.CREDENTIAL_KIND_SESSION
    )
    assert (
        SESSIONS.credential_kind({"credential_kind": "   "})
        == SESSIONS.CREDENTIAL_KIND_SESSION
    )


def test_credential_kind_normalizes_case_and_whitespace():
    """Case and surrounding whitespace are normalized before comparison."""
    assert (
        SESSIONS.credential_kind({"credential_kind": "  API_KEY  "})
        == SESSIONS.CREDENTIAL_KIND_API_KEY
    )
    assert (
        SESSIONS.credential_kind({"credential_kind": "Http_Basic"})
        == SESSIONS.CREDENTIAL_KIND_HTTP_BASIC
    )


# =====================================================================
# 10. API-key credential store (store / load / delete)
# =====================================================================
def test_store_load_delete_api_key_happy_path(
    sessions_directory, monkeypatch
):
    """Store then load returns the key; delete then load returns None."""
    _install_fake_keyring(monkeypatch)
    SESSIONS.store_api_key("datafordeler", "SECRETKEY")

    assert SESSIONS.load_api_key("datafordeler") == "SECRETKEY"

    SESSIONS.delete_api_key("datafordeler")
    assert SESSIONS.load_api_key("datafordeler") is None


def test_store_api_key_raises_without_backend(
    sessions_directory, monkeypatch
):
    """A ``keyring.backends.fail`` backend => CredentialStoreUnavailable."""
    _install_fake_keyring(monkeypatch, backend_module="keyring.backends.fail")
    with pytest.raises(SESSIONS.CredentialStoreUnavailable):
        SESSIONS.store_api_key("datafordeler", "SECRETKEY")


def test_load_api_key_none_when_keyring_errors(
    sessions_directory, monkeypatch, capsys
):
    """A ``keyring`` import that explodes is swallowed => None + warning."""
    _install_fake_keyring(monkeypatch)
    SESSIONS.store_api_key("datafordeler", "SECRETKEY")
    # Make ``import keyring`` raise ImportError inside load_api_key.
    monkeypatch.setitem(sys.modules, "keyring", None)

    assert SESSIONS.load_api_key("datafordeler") is None
    assert "WARNING" in capsys.readouterr().out


# =====================================================================
# 11. sign_in_api_key
# =====================================================================
def _api_key_definition() -> dict:
    """An api_key-kind provider definition with a validation probe."""
    return {
        "code": "DATAFORDELER",
        "session_name": "datafordeler",
        "credential_kind": "api_key",
        "api_key_probe_url": (
            "https://example.invalid/wcs?token={api_key}&request=x"
        ),
        "registration_url": "https://register.example.invalid/",
    }


def test_sign_in_api_key_empty_key_raises(sessions_directory, monkeypatch):
    """An empty (or whitespace-only) key => LoginError before any request."""
    _install_fake_keyring(monkeypatch)
    captured: List[str] = []
    monkeypatch.setattr(
        "requests.get", lambda url, **kw: captured.append(url)
    )
    with pytest.raises(SESSIONS.LoginError):
        SESSIONS.sign_in_api_key(_api_key_definition(), "   ")
    assert captured == []


def test_sign_in_api_key_substitutes_placeholder_and_stores(
    sessions_directory, monkeypatch
):
    """A 200 probe stores the key; the {api_key} placeholder is substituted."""
    store = _install_fake_keyring(monkeypatch)
    captured: Dict[str, object] = {}

    def _get(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return FakeResponse(200)

    monkeypatch.setattr("requests.get", _get)
    result = SESSIONS.sign_in_api_key(_api_key_definition(), "SECRETKEY")

    assert result == "SECRETKEY"
    # The probe URL had its {api_key} placeholder substituted, no leftover.
    assert "SECRETKEY" in captured["url"]
    assert "{api_key}" not in captured["url"]
    # The key was stored in the secret store under the api-key account.
    assert SESSIONS.load_api_key("datafordeler") == "SECRETKEY"
    assert any("SECRETKEY" == value for value in store.values())


@pytest.mark.parametrize("status", [401, 302])
def test_sign_in_api_key_rejected_status_raises(
    sessions_directory, monkeypatch, status
):
    """A signed-out probe status => LoginError mentioning the rejection."""
    _install_fake_keyring(monkeypatch)
    monkeypatch.setattr(
        "requests.get", lambda url, **kw: FakeResponse(status)
    )
    with pytest.raises(SESSIONS.LoginError) as excinfo:
        SESSIONS.sign_in_api_key(_api_key_definition(), "SECRETKEY")
    assert "rejected" in str(excinfo.value).lower()
    # A rejected key is never stored.
    assert SESSIONS.load_api_key("datafordeler") is None


def test_sign_in_api_key_without_probe_url_skips_validation(
    sessions_directory, monkeypatch
):
    """No ``api_key_probe_url`` => store the key without any request."""
    _install_fake_keyring(monkeypatch)
    captured: List[str] = []
    monkeypatch.setattr(
        "requests.get", lambda url, **kw: captured.append(url)
    )
    definition = _api_key_definition()
    definition.pop("api_key_probe_url")

    result = SESSIONS.sign_in_api_key(definition, "SECRETKEY")
    assert result == "SECRETKEY"
    assert captured == []  # no validation request was made
    assert SESSIONS.load_api_key("datafordeler") == "SECRETKEY"


def test_sign_in_api_key_without_remember_does_not_store(
    sessions_directory, monkeypatch
):
    """remember=False validates the key but never stores it."""
    _install_fake_keyring(monkeypatch)
    monkeypatch.setattr("requests.get", lambda url, **kw: FakeResponse(200))

    result = SESSIONS.sign_in_api_key(
        _api_key_definition(), "SECRETKEY", remember=False
    )
    assert result == "SECRETKEY"
    assert SESSIONS.load_api_key("datafordeler") is None


# =====================================================================
# 12. ensure_api_key
# =====================================================================
def test_ensure_api_key_returns_stored_key(sessions_directory, monkeypatch):
    """A stored key is returned verbatim."""
    _install_fake_keyring(monkeypatch)
    SESSIONS.store_api_key("datafordeler", "SECRETKEY")
    assert SESSIONS.ensure_api_key(_api_key_definition()) == "SECRETKEY"


def test_ensure_api_key_missing_raises_with_code_and_registration_url(
    sessions_directory, monkeypatch
):
    """No stored key => LoginError naming the provider and the sign-up link."""
    _install_fake_keyring(monkeypatch)  # empty store
    with pytest.raises(SESSIONS.LoginError) as excinfo:
        SESSIONS.ensure_api_key(_api_key_definition())
    message = str(excinfo.value)
    assert "DATAFORDELER" in message
    assert "https://register.example.invalid/" in message


# =====================================================================
# 13. ensure_session -- http_basic kind
# =====================================================================
def _http_basic_definition() -> dict:
    """An http_basic-kind provider definition (credentials ride every read)."""
    return {
        "code": "GEOTORGET",
        "session_name": "geotorget",
        "credential_kind": "http_basic",
        "session_probe_url": "https://example.invalid/cog/tile.tif",
        "registration_url": "https://register.example.invalid/",
    }


def test_ensure_session_http_basic_sets_auth_from_stored_credentials(
    sessions_directory, monkeypatch
):
    """Stored credentials + an ok probe => session.auth carries the tuple."""
    session = FakeSession(get_handler=lambda url, **kw: FakeResponse(200))
    monkeypatch.setattr(SESSIONS, "build_session", lambda name: session)
    monkeypatch.setattr(
        SESSIONS, "load_credentials", lambda name: ("pilot", "secret-pass")
    )

    result = SESSIONS.ensure_session(_http_basic_definition())
    assert result is session
    assert session.auth == ("pilot", "secret-pass")


def test_ensure_session_http_basic_probe_rejects_raises(
    sessions_directory, monkeypatch
):
    """Stored credentials but a 401 probe => LoginError."""
    session = FakeSession(get_handler=lambda url, **kw: FakeResponse(401))
    monkeypatch.setattr(SESSIONS, "build_session", lambda name: session)
    monkeypatch.setattr(
        SESSIONS, "load_credentials", lambda name: ("pilot", "secret-pass")
    )

    with pytest.raises(SESSIONS.LoginError):
        SESSIONS.ensure_session(_http_basic_definition())


def test_ensure_session_http_basic_no_credentials_raises_with_registration(
    sessions_directory, monkeypatch
):
    """No stored credentials => LoginError carrying the registration link."""
    session = FakeSession(get_handler=lambda url, **kw: FakeResponse(200))
    monkeypatch.setattr(SESSIONS, "build_session", lambda name: session)
    monkeypatch.setattr(SESSIONS, "load_credentials", lambda name: None)

    with pytest.raises(SESSIONS.LoginError) as excinfo:
        SESSIONS.ensure_session(_http_basic_definition())
    message = str(excinfo.value)
    assert "GEOTORGET" in message
    assert "https://register.example.invalid/" in message


# =====================================================================
# 14. sign_in -- http_basic kind
# =====================================================================
def test_sign_in_http_basic_stores_without_login_flow(
    sessions_directory, monkeypatch
):
    """An ok probe stores the credentials; no login flow is ever invoked."""
    session = FakeSession(get_handler=lambda url, **kw: FakeResponse(200))
    monkeypatch.setattr(SESSIONS, "build_session", lambda name: session)
    flow_calls: List[tuple] = []
    monkeypatch.setattr(
        SESSIONS, "run_login_flow", lambda *args: flow_calls.append(args)
    )
    stored: List[tuple] = []
    monkeypatch.setattr(
        SESSIONS,
        "store_credentials",
        lambda name, username, password: stored.append(
            (name, username, password)
        ),
    )

    # The definition carries no login_flow -- http_basic must not need one.
    result = SESSIONS.sign_in(
        _http_basic_definition(), "pilot", "secret-pass", remember=True
    )
    assert result is session
    assert session.auth == ("pilot", "secret-pass")
    assert flow_calls == []  # no registered flow was called
    assert stored == [("geotorget", "pilot", "secret-pass")]
    # http_basic never persists a cookie file (the credentials ride reads).
    assert not os.path.exists(SESSIONS.cookie_file_path("geotorget"))


def test_sign_in_http_basic_probe_rejects_raises(
    sessions_directory, monkeypatch
):
    """A 401 probe => LoginError and no flow is invoked."""
    session = FakeSession(get_handler=lambda url, **kw: FakeResponse(401))
    monkeypatch.setattr(SESSIONS, "build_session", lambda name: session)
    flow_calls: List[tuple] = []
    monkeypatch.setattr(
        SESSIONS, "run_login_flow", lambda *args: flow_calls.append(args)
    )

    with pytest.raises(SESSIONS.LoginError):
        SESSIONS.sign_in(_http_basic_definition(), "pilot", "wrong-pass")
    assert flow_calls == []


# =====================================================================
# 15. probe_signed_in sends a Range header
# =====================================================================
def test_probe_signed_in_sends_range_header():
    """The probe request carries a ``Range: bytes=0-63`` header."""
    session = FakeSession(get_handler=lambda url, **kw: FakeResponse(200))
    assert SESSIONS.probe_signed_in(session, _definition()) is True
    (_url, kwargs) = session.get_calls[0]
    assert kwargs["headers"]["Range"] == "bytes=0-63"


# =====================================================================
# 16. sign_out deletes a stored API key too
# =====================================================================
def test_sign_out_deletes_stored_api_key(sessions_directory, monkeypatch):
    """sign_out removes an api_key-kind secret alongside cookies/credentials."""
    _install_fake_keyring(monkeypatch)
    SESSIONS.store_api_key("datafordeler", "SECRETKEY")
    assert SESSIONS.load_api_key("datafordeler") == "SECRETKEY"

    SESSIONS.sign_out("datafordeler")

    assert SESSIONS.load_api_key("datafordeler") is None


# =====================================================================
# 17. setup_steps ordering / gaps / blanks
# =====================================================================
def test_setup_steps_returns_ordered_text():
    """setup_step_N keys are returned in numeric order, gaps tolerated."""
    definition = {
        "setup_step_2": "second",
        "setup_step_1": "first",
        "setup_step_4": "fourth",
        "setup_step_3": "   ",  # blank -> skipped
        "unrelated": "ignored",
    }
    assert SESSIONS.setup_steps(definition) == ["first", "second", "fourth"]


def test_setup_steps_empty_when_none_declared():
    """A definition without setup_step_N keys yields no steps."""
    assert SESSIONS.setup_steps({"code": "X"}) == []
