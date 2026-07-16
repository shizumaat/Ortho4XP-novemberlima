"""Authenticated provider sessions -- shared login + credential machinery.

Some data providers gate their downloads behind a user account (the first
is Portugal's Direcao-Geral do Territorio lidar download centre).  This
module owns everything session-related, shared by ANY provider definition
that declares a login -- elevation today, imagery or overlays tomorrow:

- a login-flow registry (mirroring the elevation access-strategy
  registry) so new authentication schemes plug in without touching any
  orchestration code;
- the ``keycloak_password`` flow: a scripted Keycloak username/password
  sign-in (fetch the hosted login form, submit credentials, follow the
  authorization-code redirect chain until the site session cookie is
  set).  Works for any Keycloak-fronted service without a CAPTCHA;
- cookie persistence as Netscape-format cookie files under the data
  root, readable both by ``requests`` (http.cookiejar.MozillaCookieJar)
  and by GDAL ``/vsicurl`` (the ``GDAL_HTTP_COOKIEFILE`` option), so the
  GUI process can sign in and the tile-build subprocess can reuse the
  session with no GUI involvement;
- credential storage through the optional ``keyring`` package, which
  backs onto the platform secret store: macOS Keychain, Windows
  Credential Locker, or the Linux Secret Service (GNOME Keyring /
  KWallet).  Passwords never touch a configuration file or the cookie
  directory.

Session definitions are plain dictionaries (in practice: parsed ``.elv``
provider definitions) using these keys:

    session_name        stable identifier shared by every provider that
                        uses the same account (e.g. "dgterritorio")
    credential_kind     "session" (default: cookie session established
                        by a login flow), "http_basic" (username and
                        password sent as HTTP Basic authentication on
                        the data reads themselves), or "api_key" (one
                        secret string, substituted for the {api_key}
                        placeholder in the provider's URLs)
    login_flow          [session kind] registered flow name
                        (e.g. "keycloak_password")
    login_url           [session kind] where the flow starts (the
                        service's sign-in entry point, NOT the identity
                        provider's)
    session_probe_url   [session/http_basic kinds] URL whose response
                        distinguishes signed-in from signed-out
                        (redirect/401/403 = signed out)
    api_key_probe_url   [api_key kind] URL with an {api_key}
                        placeholder used to validate a key at sign-in
                        time (same signed-out convention)
    registration_url    where a user can CREATE the account; shown in
                        sign-in prompts and error messages

Core module: no GUI-toolkit imports.  The Qt settings window calls
:func:`sign_in` / :func:`sign_out` / :func:`signed_in`; build-time code
calls :func:`ensure_session` and gets back a ready ``requests.Session``.
"""

import html
import html.parser
import http.cookiejar
import json
import os
import re
import stat

import O4_File_Names as FNAMES
import O4_UI_Utils as UI

# Registered login flows: name -> callable(session, definition, username,
# password).  A flow mutates the session (cookies) and raises LoginError
# on failure; its return value is ignored.
LOGIN_FLOWS = {}

_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_SIGNED_OUT_STATUSES = _REDIRECT_STATUSES + (401, 403)

_USER_AGENT = "Ortho4XP"

# Service-name prefix for the platform secret store.  The visible entry
# on macOS Keychain / Windows Credential Locker / Linux Secret Service
# reads e.g. "Ortho4XP session dgterritorio".
_CREDENTIAL_SERVICE_PREFIX = "Ortho4XP session "

# The three credential kinds a definition can declare (see module
# docstring); "session" is the default when the key is absent.
CREDENTIAL_KIND_SESSION = "session"
CREDENTIAL_KIND_HTTP_BASIC = "http_basic"
CREDENTIAL_KIND_API_KEY = "api_key"

# Secret-store account name under which an api_key-kind secret is filed
# (there is no username; the key IS the whole credential).
_API_KEY_ACCOUNT = "api-key"


def credential_kind(definition):
    """The definition's declared credential kind (default: session)."""
    return (
        str(definition.get("credential_kind", CREDENTIAL_KIND_SESSION))
        .strip()
        .lower()
        or CREDENTIAL_KIND_SESSION
    )


def _account_hint(definition):
    """Sign-in guidance for error messages, with the registration link."""
    hint = (
        "Open Settings and sign in to this provider (a free account "
        "with the data service is required"
    )
    registration_url = definition.get("registration_url")
    if registration_url:
        hint += "; create one at %s" % registration_url
    return hint + ")."


_SETUP_STEP_KEY = re.compile(r"^setup_step_(\d+)$")


def setup_steps(definition):
    """Ordered human setup instructions declared by a provider definition.

    A definition may carry ``setup_step_1``, ``setup_step_2``, ... keys
    (parsed verbatim from the ``.elv`` file); this returns their text in
    numeric order, skipping blanks and tolerating gaps in the numbering.
    The settings sign-in dialog renders the result as a numbered
    checklist so an account that needs an extra step (Sweden's product
    order, Denmark's token copy) tells the user exactly what to do
    before typing credentials.  Empty when the definition declares none.
    """
    numbered = []
    for key, value in definition.items():
        match = _SETUP_STEP_KEY.match(str(key))
        if match and str(value).strip():
            numbered.append((int(match.group(1)), str(value).strip()))
    numbered.sort(key=lambda pair: pair[0])
    return [text for _number, text in numbered]


class LoginError(Exception):
    """A sign-in problem whose message is safe to show the user.

    Messages never contain the password.
    """


class CredentialStoreUnavailable(LoginError):
    """No usable platform secret store (keyring missing or backend-less)."""


def register_login_flow(name):
    """Decorator adding a login flow to the registry (see LOGIN_FLOWS)."""

    def _register(flow):
        LOGIN_FLOWS[name] = flow
        return flow

    return _register


# =====================================================================
# Cookie-file persistence (Netscape format, shared with GDAL /vsicurl)
# =====================================================================
def sessions_directory():
    """Writable directory for session cookie files, under the data root."""
    return FNAMES.data_path("Sessions")


def cookie_file_path(session_name):
    """Netscape-format cookie file for a named session.

    The format is what GDAL's ``GDAL_HTTP_COOKIEFILE`` option expects, so
    a strategy whose remote reads themselves need the session can hand
    this path straight to GDAL.
    """
    return os.path.join(sessions_directory(), session_name + ".cookies.txt")


def _account_file_path(session_name):
    # The username is not a secret; it lives next to the cookie file so
    # load_credentials() knows which secret-store entry to ask for.
    return os.path.join(sessions_directory(), session_name + ".account.json")


def _restrict_to_owner(path, directory=False):
    try:
        os.chmod(
            path,
            (stat.S_IRWXU) if directory else (stat.S_IRUSR | stat.S_IWUSR),
        )
    except OSError:
        # Permission bits are advisory hardening; never fatal (e.g. some
        # Windows filesystems).
        pass


def build_session(session_name):
    """A requests.Session carrying the named session's persisted cookies.

    Always returns a session (empty cookie jar when none persisted yet).
    """
    import requests

    session = requests.Session()
    session.headers["User-Agent"] = _USER_AGENT
    jar = http.cookiejar.MozillaCookieJar(cookie_file_path(session_name))
    try:
        # ignore_discard: the site session cookies we live off are
        # browser-session cookies (no expiry stamp) -- without this flag
        # the jar would drop exactly the cookies we persist.
        jar.load(ignore_discard=True)
    except (FileNotFoundError, http.cookiejar.LoadError, OSError):
        pass
    session.cookies = jar
    return session


def save_session_cookies(session, session_name):
    """Persist a session's cookies to the named Netscape cookie file."""
    directory = sessions_directory()
    os.makedirs(directory, exist_ok=True)
    _restrict_to_owner(directory, directory=True)
    path = cookie_file_path(session_name)
    jar = http.cookiejar.MozillaCookieJar(path)
    for cookie in session.cookies:
        jar.set_cookie(cookie)
    jar.save(ignore_discard=True)
    _restrict_to_owner(path)


# =====================================================================
# Credential storage (platform secret store via keyring)
# =====================================================================
def credential_store_available():
    """Whether a platform secret store is usable on this machine."""
    try:
        import keyring
        import keyring.errors

        backend = keyring.get_keyring()
    except Exception:
        return False
    # keyring's "fail" backend advertises priority 0 and raises on use.
    return type(backend).__module__ != "keyring.backends.fail"


def _credential_service(session_name):
    return _CREDENTIAL_SERVICE_PREFIX + session_name


def store_credentials(session_name, username, password):
    """Store account credentials in the platform secret store.

    The username lands in a plain JSON sidecar (it is not a secret and
    load_credentials() needs it to query the store); the password goes to
    macOS Keychain / Windows Credential Locker / Linux Secret Service.
    Raises CredentialStoreUnavailable when no secret store is usable.
    """
    if not credential_store_available():
        raise CredentialStoreUnavailable(
            "No platform secret store is available (the 'keyring' package "
            "is missing or has no usable backend); credentials were NOT "
            "saved.  Sign-in can still proceed for this run."
        )
    import keyring

    directory = sessions_directory()
    os.makedirs(directory, exist_ok=True)
    _restrict_to_owner(directory, directory=True)
    keyring.set_password(_credential_service(session_name), username, password)
    account_path = _account_file_path(session_name)
    with open(account_path, "w", encoding="utf-8") as handle:
        json.dump({"username": username}, handle)
    _restrict_to_owner(account_path)


def load_credentials(session_name):
    """Return (username, password) from the secret store, or None."""
    try:
        with open(
            _account_file_path(session_name), "r", encoding="utf-8"
        ) as handle:
            username = json.load(handle).get("username")
    except (FileNotFoundError, ValueError, OSError):
        return None
    if not username:
        return None
    try:
        import keyring

        password = keyring.get_password(
            _credential_service(session_name), username
        )
    except Exception as error:
        UI.vprint(
            1,
            "   WARNING: could not read the platform secret store for "
            "session",
            session_name,
            ":",
            str(error),
        )
        return None
    if password is None:
        return None
    return (username, password)


def delete_credentials(session_name):
    """Remove stored credentials (secret store entry + username sidecar)."""
    credentials = load_credentials(session_name)
    if credentials is not None:
        try:
            import keyring

            keyring.delete_password(
                _credential_service(session_name), credentials[0]
            )
        except Exception:
            pass
    try:
        os.remove(_account_file_path(session_name))
    except OSError:
        pass


def store_api_key(session_name, api_key):
    """Store an api_key-kind secret in the platform secret store."""
    if not credential_store_available():
        raise CredentialStoreUnavailable(
            "No platform secret store is available (the 'keyring' package "
            "is missing or has no usable backend); the API key was NOT "
            "saved."
        )
    import keyring

    keyring.set_password(
        _credential_service(session_name), _API_KEY_ACCOUNT, api_key
    )


def load_api_key(session_name):
    """Return the stored API key for a session, or None."""
    try:
        import keyring

        return keyring.get_password(
            _credential_service(session_name), _API_KEY_ACCOUNT
        )
    except Exception as error:
        UI.vprint(
            1,
            "   WARNING: could not read the platform secret store for "
            "session",
            session_name,
            ":",
            str(error),
        )
        return None


def delete_api_key(session_name):
    """Remove a stored API key (no-op when none is stored)."""
    try:
        import keyring

        keyring.delete_password(
            _credential_service(session_name), _API_KEY_ACCOUNT
        )
    except Exception:
        pass


# =====================================================================
# Probe / ensure / sign-in / sign-out
# =====================================================================
def probe_signed_in(session, definition):
    """Whether the session is currently signed in at the provider.

    Convention: the probe URL answers a redirect (to a login page) or
    401/403 when signed out, and anything else -- 200 or even 404 --
    when signed in.  Network trouble counts as signed out so callers
    re-login rather than proceed with a session they cannot verify.
    """
    probe_url = definition.get("session_probe_url")
    if not probe_url:
        return False
    try:
        # The Range header keeps probes against large data files (some
        # services only gate the files themselves) down to a few bytes;
        # services that ignore Range just answer 200 as usual.
        response = session.get(
            probe_url,
            timeout=30,
            allow_redirects=False,
            headers={"Range": "bytes=0-63"},
        )
    except Exception:
        return False
    return response.status_code not in _SIGNED_OUT_STATUSES


def signed_in(definition):
    """Probe the persisted session without side effects (GUI status)."""
    session_name = definition.get("session_name")
    if not session_name:
        return False
    return probe_signed_in(build_session(session_name), definition)


def run_login_flow(session, definition, username, password):
    """Execute the definition's registered login flow on a session."""
    flow_name = definition.get("login_flow")
    flow = LOGIN_FLOWS.get(flow_name)
    if flow is None:
        raise LoginError(
            "Unknown login flow '%s' for session '%s'."
            % (flow_name, definition.get("session_name"))
        )
    flow(session, definition, username, password)


def sign_in(definition, username, password, remember=True):
    """Interactive entry point (settings UI): sign in and persist.

    ``session`` kind: runs the login flow with the supplied credentials,
    verifies with the probe, and persists the session cookies.
    ``http_basic`` kind: there is no login flow -- the credentials ride
    every data read -- so sign-in just verifies them against the probe.
    Either way, when ``remember`` and a secret store is available the
    credentials are stored for automatic use later.  Raises LoginError
    (message safe to display) on failure.  api_key-kind definitions use
    :func:`sign_in_api_key` instead.
    """
    session_name = definition.get("session_name")
    if not session_name:
        raise LoginError("Provider definition lacks a session_name.")
    kind = credential_kind(definition)
    session = build_session(session_name)
    if kind == CREDENTIAL_KIND_HTTP_BASIC:
        session.auth = (username, password)
        if not probe_signed_in(session, definition):
            raise LoginError(
                "The service rejected these credentials (wrong username "
                "or password?)."
            )
    else:
        session.cookies.clear()
        run_login_flow(session, definition, username, password)
        if not probe_signed_in(session, definition):
            raise LoginError(
                "Sign-in did not produce a working session (the service "
                "accepted the form but the probe still reports signed "
                "out)."
            )
        save_session_cookies(session, session_name)
    if remember:
        try:
            store_credentials(session_name, username, password)
        except CredentialStoreUnavailable as error:
            # The sign-in itself worked; a missing secret store only
            # costs automatic re-login when the session expires.
            UI.vprint(1, "   WARNING:", str(error))
    return session


def sign_in_api_key(definition, api_key, remember=True):
    """Interactive entry point for api_key-kind providers.

    Validates the key against ``api_key_probe_url`` (with the
    ``{api_key}`` placeholder substituted; a redirect/401/403 answer
    means the key is bad) and stores it in the platform secret store.
    """
    import requests

    session_name = definition.get("session_name")
    if not session_name:
        raise LoginError("Provider definition lacks a session_name.")
    api_key = (api_key or "").strip()
    if not api_key:
        raise LoginError("Enter an API key.")
    probe_template = definition.get("api_key_probe_url")
    if probe_template:
        try:
            response = requests.get(
                probe_template.replace("{api_key}", api_key),
                timeout=30,
                allow_redirects=False,
                headers={"User-Agent": _USER_AGENT},
            )
        except Exception as error:
            raise LoginError(
                "Could not validate the API key: %s" % str(error)
            )
        if response.status_code in _SIGNED_OUT_STATUSES:
            raise LoginError(
                "The service rejected this API key (status %d)."
                % response.status_code
            )
    if remember:
        store_api_key(session_name, api_key)
    return api_key


def sign_out(session_name):
    """Forget the persisted session and any stored credentials."""
    delete_credentials(session_name)
    delete_api_key(session_name)
    try:
        os.remove(cookie_file_path(session_name))
    except OSError:
        pass


def ensure_session(definition, credentials=None):
    """Build-time entry point: a signed-in session, re-logging as needed.

    ``session`` kind: persisted cookies first (probe), then an automatic
    re-login with ``credentials`` or the secret store's stored account.
    ``http_basic`` kind: a session carrying the stored credentials as
    HTTP Basic authentication (verified against the probe).  Raises
    LoginError with sign-in instructions when nothing works -- callers
    surface the message and treat the provider as no-coverage.
    """
    session_name = definition.get("session_name")
    if not session_name:
        raise LoginError("Provider definition lacks a session_name.")
    kind = credential_kind(definition)
    session = build_session(session_name)
    if kind == CREDENTIAL_KIND_HTTP_BASIC:
        if credentials is None:
            credentials = load_credentials(session_name)
        if credentials is None:
            raise LoginError(
                "Provider '%s' needs a '%s' account and no stored "
                "sign-in was found.  %s"
                % (
                    definition.get("code", "?"),
                    session_name,
                    _account_hint(definition),
                )
            )
        session.auth = tuple(credentials)
        if not probe_signed_in(session, definition):
            raise LoginError(
                "The stored '%s' credentials were rejected by the "
                "service.  Open Settings and sign in again."
                % session_name
            )
        return session
    if probe_signed_in(session, definition):
        return session
    if credentials is None:
        credentials = load_credentials(session_name)
    if credentials is None:
        raise LoginError(
            "Provider '%s' needs a signed-in '%s' session and no stored "
            "sign-in was found.  %s"
            % (
                definition.get("code", "?"),
                session_name,
                _account_hint(definition),
            )
        )
    (username, password) = credentials
    UI.vprint(
        1,
        "   Session",
        session_name,
        "expired - signing in again as",
        username,
    )
    session.cookies.clear()
    run_login_flow(session, definition, username, password)
    if not probe_signed_in(session, definition):
        raise LoginError(
            "Automatic re-login for session '%s' failed (stored "
            "credentials may be stale).  Open Settings and sign in "
            "again." % session_name
        )
    save_session_cookies(session, session_name)
    return session


def ensure_api_key(definition):
    """Build-time entry point for api_key-kind providers.

    Returns the stored key, or raises LoginError with sign-in (and
    account-creation) instructions.  No probe here: the key rides the
    data requests themselves, which fail loudly on a stale key.
    """
    session_name = definition.get("session_name")
    if not session_name:
        raise LoginError("Provider definition lacks a session_name.")
    api_key = load_api_key(session_name)
    if not api_key:
        raise LoginError(
            "Provider '%s' needs a '%s' API key and none is stored.  %s"
            % (
                definition.get("code", "?"),
                session_name,
                _account_hint(definition),
            )
        )
    return api_key


# =====================================================================
# Login flow: keycloak_password
# =====================================================================
class _LoginFormParser(html.parser.HTMLParser):
    """Extract the first form carrying a password input.

    Keycloak's hosted login page is plain server-rendered HTML: one form
    (id ``kc-form-login``) whose action URL carries the one-time
    session_code/execution parameters, a username input, a password
    input, and sometimes hidden inputs (credentialId) that must be sent
    back verbatim.
    """

    def __init__(self):
        super().__init__()
        self._depth_in_form = 0
        self._current = None
        self.forms = []  # [{action, inputs:{name: value}, has_password}]

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == "form":
            self._current = {
                "action": html.unescape(attributes.get("action", "") or ""),
                "inputs": {},
                "has_password": False,
            }
            self.forms.append(self._current)
        elif tag == "input" and self._current is not None:
            name = attributes.get("name")
            input_type = (attributes.get("type") or "text").lower()
            if input_type == "password":
                self._current["has_password"] = True
            if name and input_type not in ("submit", "button", "password"):
                self._current["inputs"][name] = attributes.get("value", "") or ""

    def handle_endtag(self, tag):
        if tag == "form":
            self._current = None


def _find_password_form(page_html):
    parser = _LoginFormParser()
    try:
        parser.feed(page_html)
    except Exception:
        return None
    for form in parser.forms:
        if form["has_password"] and form["action"]:
            return form
    return None


@register_login_flow("keycloak_password")
def keycloak_password_login(session, definition, username, password):
    """Scripted Keycloak sign-in via the service's login entry point.

    GET ``login_url`` and follow redirects to the Keycloak-hosted form
    (this leg establishes the authorization-code + PKCE state -- the
    service's own proxy generated it, we merely carry cookies).  POST
    the credentials to the form's action URL and follow the redirect
    chain back to the service, whose callback sets the session cookie.
    A response that still contains a password form means the identity
    provider re-rendered the login page: wrong credentials (or an
    unsupported challenge such as a one-time code).
    """
    login_url = definition.get("login_url")
    if not login_url:
        raise LoginError("Provider definition lacks a login_url.")
    try:
        response = session.get(login_url, timeout=30)
    except Exception as error:
        raise LoginError(
            "Could not reach the sign-in page: %s" % str(error)
        )
    form = _find_password_form(response.text)
    if form is None:
        raise LoginError(
            "No password form found at the sign-in page (the service may "
            "have changed its login flow, or it now requires a browser)."
        )
    form_data = dict(form["inputs"])
    form_data["username"] = username
    form_data["password"] = password
    try:
        response = session.post(form["action"], data=form_data, timeout=30)
    except Exception as error:
        raise LoginError("Submitting credentials failed: %s" % str(error))
    if _find_password_form(response.text) is not None:
        raise LoginError(
            "The identity provider rejected the sign-in (wrong username "
            "or password, or an additional challenge is required)."
        )
