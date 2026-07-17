"""Unit tests for the ``authenticated_token_search`` access strategy.

The strategy lives in :mod:`O4_Airport_Elevation_Insets` as
``AuthenticatedTokenSearchStrategy`` (registered under
``ACCESS_STRATEGIES['authenticated_token_search']``).  It performs a
public OGC/STAC search, then redeems each item's tokenized download href
through a signed-in session into a presigned object-storage URL that GDAL
reads windowed via ``/vsicurl``.

All tests are headless and issue no request: ``requests.post`` is
monkeypatched for discovery, the session is a fake, and GDAL is stubbed
out (``has_gdal`` forced, ``warp_vsicurl_sources_to_geotiff`` and
``_geotiff_has_valid_data`` replaced).  The definition below mirrors the
shape of ``PORTUGAL50CM.elv`` but uses ``example.invalid`` URLs so the
tests stay independent of any provider file.

Despite the file name, the later sections here also cover the OTHER
credential-gated access-strategy fetch paths that share the same
``O4_Authenticated_Sessions`` machinery: :class:`WcsStrategy` (api_key
kind: an ``{api_key}`` placeholder substituted into the WCS service URL)
and :class:`StacCloudOptimizedGeoTiffStrategy` (http_basic kind:
``GDAL_HTTP_USERPWD`` passed through the warp-scoped GDAL configuration).
The invariant these tests protect is the same one the token-search tests
do -- the secret rides the GDAL read but never reaches the provenance
record.
"""

import json
from typing import Callable, Dict, List, Optional

import pytest

import O4_Airport_Elevation_Insets as INSETS
import O4_Authenticated_Sessions as SESSIONS


# =====================================================================
# Shared fixtures and fakes
# =====================================================================
def _definition() -> dict:
    """An ``authenticated_token_search`` definition (example.invalid URLs)."""
    return {
        "code": "PORTUGAL50CM",
        "access_strategy": "authenticated_token_search",
        "search_url": "https://example.invalid/backend/v1/search",
        "collections": "MDT-50cm",
        "search_limit": "200",
        "session_name": "dgterritorio",
        "login_flow": "keycloak_password",
        "login_url": "https://example.invalid/auth/login",
        "session_probe_url": "https://example.invalid/backend/v1/?f=json",
        "native_resolution_m": "0.5",
        "coverage_bbox": (-9.6, 36.9, -6.1, 42.2),
        "source_nodata": "-999",
        "vertical_datum": "Cascais",
        "license": "CC BY 4.0",
        "attribution": "Direcao-Geral do Territorio",
    }


def _strategy() -> INSETS.AuthenticatedTokenSearchStrategy:
    return INSETS.ACCESS_STRATEGIES["authenticated_token_search"]()


def _feature_collection() -> dict:
    """A two-item STAC ItemCollection with tokenized data assets."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "id": "item-1",
                "assets": {
                    "data": {
                        "href": "https://example.invalid/download/token-1",
                        "roles": ["data"],
                    }
                },
            },
            {
                "id": "item-2",
                "assets": {
                    "data": {
                        "href": "https://example.invalid/download/token-2",
                        "roles": ["data"],
                    }
                },
            },
        ],
    }


class FakeResponse:
    """Minimal stand-in for a ``requests`` response."""

    def __init__(
        self,
        status_code: int = 200,
        headers: Optional[Dict[str, str]] = None,
        payload: Optional[dict] = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers if headers is not None else {}
        self._payload = payload

    def json(self) -> dict:
        return self._payload if self._payload is not None else {}


class FakeSession:
    """Record-and-reply session whose GET redeems hrefs by a mapping."""

    def __init__(
        self, get_handler: Callable[..., FakeResponse]
    ) -> None:
        self._get_handler = get_handler
        self.get_calls: List[str] = []

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.get_calls.append(url)
        return self._get_handler(url, **kwargs)


# =====================================================================
# 1. discover()
# =====================================================================
def test_discover_posts_expected_body_and_returns_features(monkeypatch):
    """The POST body carries bbox/limit/collections; features come back."""
    captured: Dict[str, object] = {}

    def _post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return FakeResponse(200, payload=_feature_collection())

    monkeypatch.setattr("requests.post", _post)
    bounding_box = (-9.2, 38.6, -9.0, 38.8)
    items = _strategy().discover(_definition(), bounding_box)

    assert [item["id"] for item in items] == ["item-1", "item-2"]
    body = captured["json"]
    assert body["bbox"] == [-9.2, 38.6, -9.0, 38.8]
    assert isinstance(body["bbox"], list)
    assert isinstance(body["limit"], int)
    assert body["collections"] == ["MDT-50cm"]
    assert isinstance(body["collections"], list)


def test_discover_returns_none_outside_coverage(monkeypatch):
    """A window outside ``coverage_bbox`` short-circuits (no request)."""

    def _post(url, json=None, timeout=None):
        raise AssertionError("no request outside coverage")

    monkeypatch.setattr("requests.post", _post)
    # Well east of mainland Portugal's coverage box.
    outside = (10.0, 45.0, 11.0, 46.0)
    assert _strategy().discover(_definition(), outside) is None


def test_discover_returns_none_on_non_200(monkeypatch):
    """A non-200 search response yields None."""

    def _post(url, json=None, timeout=None):
        return FakeResponse(503, payload={})

    monkeypatch.setattr("requests.post", _post)
    bounding_box = (-9.2, 38.6, -9.0, 38.8)
    assert _strategy().discover(_definition(), bounding_box) is None


def test_discover_warns_on_full_page(monkeypatch, capsys):
    """A full page (len == limit) emits the incomplete-coverage warning."""

    def _post(url, json=None, timeout=None):
        return FakeResponse(200, payload=_feature_collection())

    monkeypatch.setattr("requests.post", _post)
    definition = _definition()
    definition["search_limit"] = "2"  # two features == full page
    bounding_box = (-9.2, 38.6, -9.0, 38.8)
    items = _strategy().discover(definition, bounding_box)

    assert len(items) == 2
    output = capsys.readouterr().out
    assert "full page" in output


# =====================================================================
# 2. _item_download_hrefs
# =====================================================================
def test_item_download_hrefs_prefers_data_role():
    """The 'data'-role asset wins over other assets on the same item."""
    items = [
        {
            "id": "item-1",
            "assets": {
                "thumbnail": {
                    "href": "https://example.invalid/thumb.png",
                    "roles": ["thumbnail"],
                },
                "data": {
                    "href": "https://example.invalid/download/token-1",
                    "roles": ["data"],
                },
            },
        }
    ]
    hrefs = INSETS.AuthenticatedTokenSearchStrategy._item_download_hrefs(items)
    assert hrefs == ["https://example.invalid/download/token-1"]


def test_item_download_hrefs_single_asset_fallback():
    """A lone asset without roles is used when it is the only one."""
    items = [
        {
            "id": "item-1",
            "assets": {
                "only": {"href": "https://example.invalid/download/token-1"}
            },
        }
    ]
    hrefs = INSETS.AuthenticatedTokenSearchStrategy._item_download_hrefs(items)
    assert hrefs == ["https://example.invalid/download/token-1"]


def test_item_download_hrefs_skips_malformed_items():
    """Items without a usable assets dict are skipped silently."""
    items = [
        {"id": "no-assets"},
        {"id": "assets-not-dict", "assets": ["not", "a", "dict"]},
        {"id": "asset-not-dict", "assets": {"data": "not-a-dict"}},
        {
            "id": "good",
            "assets": {
                "data": {
                    "href": "https://example.invalid/download/token-good",
                    "roles": ["data"],
                }
            },
        },
    ]
    hrefs = INSETS.AuthenticatedTokenSearchStrategy._item_download_hrefs(items)
    assert hrefs == ["https://example.invalid/download/token-good"]


# =====================================================================
# 3. _redeem_download_href
# =====================================================================
def test_redeem_href_returns_http_location():
    """A redirect to an http(s) Location is the presigned target."""
    presigned = "https://storage.example.invalid/presigned/1?sig=abc"

    def _get(url, **kwargs):
        return FakeResponse(302, headers={"Location": presigned})

    session = FakeSession(_get)
    result = INSETS.AuthenticatedTokenSearchStrategy._redeem_download_href(
        session, "https://example.invalid/download/token-1"
    )
    assert result == presigned


def test_redeem_href_none_on_200():
    """A 200 (the service streamed the file itself) yields None."""

    def _get(url, **kwargs):
        return FakeResponse(200)

    session = FakeSession(_get)
    result = INSETS.AuthenticatedTokenSearchStrategy._redeem_download_href(
        session, "https://example.invalid/download/token-1"
    )
    assert result is None


def test_redeem_href_none_on_non_http_location():
    """A redirect Location that is not http(s) yields None."""

    def _get(url, **kwargs):
        return FakeResponse(302, headers={"Location": "ftp://example.invalid/x"})

    session = FakeSession(_get)
    result = INSETS.AuthenticatedTokenSearchStrategy._redeem_download_href(
        session, "https://example.invalid/download/token-1"
    )
    assert result is None


def test_redeem_href_none_on_request_error():
    """A raised GET yields None."""

    def _get(url, **kwargs):
        raise ConnectionError("token aged out")

    session = FakeSession(_get)
    result = INSETS.AuthenticatedTokenSearchStrategy._redeem_download_href(
        session, "https://example.invalid/download/token-1"
    )
    assert result is None


# =====================================================================
# 4. fetch()
# =====================================================================
@pytest.fixture
def gdal_stub(monkeypatch):
    """Force ``has_gdal`` on and clear the once-per-provider warning set."""
    monkeypatch.setattr(INSETS, "has_gdal", True)
    INSETS._SIGN_IN_WARNED_PROVIDERS.clear()
    yield
    INSETS._SIGN_IN_WARNED_PROVIDERS.clear()


def _install_discover(monkeypatch):
    """Make the strategy's discover return the canned two-item list."""

    def _post(url, json=None, timeout=None):
        return FakeResponse(200, payload=_feature_collection())

    monkeypatch.setattr("requests.post", _post)


def test_fetch_login_error_warns_exactly_once(monkeypatch, gdal_stub, capsys):
    """A LoginError degrades to no-coverage and warns ONCE across two calls."""
    _install_discover(monkeypatch)

    def _ensure_session(definition, credentials=None):
        raise SESSIONS.LoginError(
            "Provider 'PORTUGAL50CM' needs a signed-in session; open Settings."
        )

    monkeypatch.setattr(SESSIONS, "ensure_session", _ensure_session)
    strategy = _strategy()
    bounding_box = (-9.2, 38.6, -9.0, 38.8)

    first = strategy.fetch(_definition(), bounding_box, 1.0, "/tmp/out.tif")
    second = strategy.fetch(_definition(), bounding_box, 1.0, "/tmp/out.tif")
    assert first is None
    assert second is None
    assert capsys.readouterr().out.count("WARNING") == 1


def test_fetch_happy_path_provenance_hides_presigned_urls(
    monkeypatch, gdal_stub, tmp_path
):
    """Each href redeems to a presigned URL warped via /vsicurl; provenance
    records item ids only, never a presigned (signed, expiring) URL."""
    _install_discover(monkeypatch)
    presigned_by_href = {
        "https://example.invalid/download/token-1": (
            "https://storage.example.invalid/presigned/1?sig=aaa"
        ),
        "https://example.invalid/download/token-2": (
            "https://storage.example.invalid/presigned/2?sig=bbb"
        ),
    }

    def _get(url, **kwargs):
        return FakeResponse(
            302, headers={"Location": presigned_by_href[url]}
        )

    monkeypatch.setattr(
        SESSIONS, "ensure_session", lambda d, credentials=None: FakeSession(_get)
    )

    captured_inputs: List[List[str]] = []

    def _warp(vsicurl_inputs, *args, **kwargs):
        captured_inputs.append(list(vsicurl_inputs))
        return True

    monkeypatch.setattr(INSETS, "warp_vsicurl_sources_to_geotiff", _warp)
    monkeypatch.setattr(INSETS, "_geotiff_has_valid_data", lambda path: True)

    destination = str(tmp_path / "out.tif")
    bounding_box = (-9.2, 38.6, -9.0, 38.8)
    provenance = _strategy().fetch(_definition(), bounding_box, 1.0, destination)

    assert provenance is not None
    assert provenance["source_ids"] == ["item-1", "item-2"]

    # Every warp input is the /vsicurl-wrapped presigned URL.
    assert captured_inputs[0] == [
        "/vsicurl/" + presigned_by_href["https://example.invalid/download/token-1"],
        "/vsicurl/" + presigned_by_href["https://example.invalid/download/token-2"],
    ]

    # No presigned URL (nor its signature) leaks into the provenance.
    serialized = json.dumps(provenance)
    for presigned in presigned_by_href.values():
        assert presigned not in serialized
    assert "sig=" not in serialized


def test_fetch_returns_none_when_warp_fails(
    monkeypatch, gdal_stub, tmp_path
):
    """A GDAL warp failure yields None."""
    _install_discover(monkeypatch)

    def _get(url, **kwargs):
        return FakeResponse(
            302,
            headers={"Location": "https://storage.example.invalid/presigned/1"},
        )

    monkeypatch.setattr(
        SESSIONS, "ensure_session", lambda d, credentials=None: FakeSession(_get)
    )
    monkeypatch.setattr(
        INSETS, "warp_vsicurl_sources_to_geotiff", lambda *a, **k: False
    )

    destination = str(tmp_path / "out.tif")
    bounding_box = (-9.2, 38.6, -9.0, 38.8)
    assert (
        _strategy().fetch(_definition(), bounding_box, 1.0, destination)
        is None
    )


def test_fetch_returns_none_when_all_redemptions_fail(
    monkeypatch, gdal_stub, tmp_path
):
    """If no token redeems, there is nothing to warp => None."""
    _install_discover(monkeypatch)

    def _get(url, **kwargs):
        # 200 means the token did not redirect to a presigned URL.
        return FakeResponse(200)

    warp_calls: List[tuple] = []
    monkeypatch.setattr(
        SESSIONS, "ensure_session", lambda d, credentials=None: FakeSession(_get)
    )
    monkeypatch.setattr(
        INSETS,
        "warp_vsicurl_sources_to_geotiff",
        lambda *a, **k: warp_calls.append(a) or True,
    )

    destination = str(tmp_path / "out.tif")
    bounding_box = (-9.2, 38.6, -9.0, 38.8)
    assert (
        _strategy().fetch(_definition(), bounding_box, 1.0, destination)
        is None
    )
    assert warp_calls == []  # warp never reached


# =====================================================================
# 5. WcsStrategy -- api_key kind ({api_key} substituted into the URL)
# =====================================================================
def _wcs_definition() -> dict:
    """A ``wcs`` definition whose service URL is api_key-gated."""
    return {
        "code": "DATAFORDELER",
        "access_strategy": "wcs",
        "wcs_service_url": (
            "https://example.invalid/dhm?token={api_key}&service=WCS"
        ),
        "wcs_version": "2.0.1",
        "wcs_coverage": "dhm_terraen",
        "session_name": "datafordeler",
        "native_resolution_m": "0.4",
        "coverage_bbox": (7.0, 54.5, 15.5, 57.9),
        "license": "Open data",
        "attribution": "Styrelsen for Dataforsyning og Infrastruktur",
    }


def test_wcs_dataset_name_leaves_placeholder_literal_without_key():
    """Without a key the ``{api_key}`` placeholder stays literal."""
    dataset = INSETS.WcsStrategy().dataset_name(_wcs_definition())
    assert "{api_key}" in dataset


def test_wcs_dataset_name_substitutes_key():
    """With a key the placeholder is substituted and no longer present."""
    dataset = INSETS.WcsStrategy().dataset_name(
        _wcs_definition(), api_key="SECRETKEY"
    )
    assert "SECRETKEY" in dataset
    assert "{api_key}" not in dataset


def test_wcs_fetch_login_error_warns_exactly_once(
    monkeypatch, gdal_stub, capsys, tmp_path
):
    """A missing api_key degrades to no-coverage and warns ONCE."""

    def _ensure_api_key(definition):
        raise SESSIONS.LoginError(
            "Provider 'DATAFORDELER' needs an API key; open Settings."
        )

    monkeypatch.setattr(SESSIONS, "ensure_api_key", _ensure_api_key)
    warp_calls: List[tuple] = []
    monkeypatch.setattr(
        INSETS,
        "warp_vsicurl_sources_to_geotiff",
        lambda *a, **k: warp_calls.append(a) or True,
    )

    strategy = INSETS.WcsStrategy()
    destination = str(tmp_path / "out.tif")
    bounding_box = (8.0, 55.0, 8.2, 55.2)
    first = strategy.fetch(_wcs_definition(), bounding_box, 1.0, destination)
    second = strategy.fetch(_wcs_definition(), bounding_box, 1.0, destination)

    assert first is None
    assert second is None
    assert warp_calls == []  # nothing warped without a key
    assert capsys.readouterr().out.count("WARNING") == 1


def test_wcs_fetch_happy_path_provenance_hides_api_key(
    monkeypatch, gdal_stub, tmp_path
):
    """The key rides the coverage open but never lands in the provenance."""
    monkeypatch.setattr(
        SESSIONS, "ensure_api_key", lambda definition: "SECRETKEY"
    )

    opened_names: List[str] = []
    opened_dataset = object()

    def _open(dataset_name, flags=0, open_options=None, **kwargs):
        opened_names.append(dataset_name)
        return opened_dataset

    monkeypatch.setattr(INSETS.gdal, "OpenEx", _open)

    captured_inputs: List[list] = []

    def _warp(vsicurl_inputs, *args, **kwargs):
        captured_inputs.append(list(vsicurl_inputs))
        return True

    monkeypatch.setattr(INSETS, "warp_vsicurl_sources_to_geotiff", _warp)
    monkeypatch.setattr(INSETS, "_geotiff_has_valid_data", lambda path: True)

    destination = str(tmp_path / "out.tif")
    bounding_box = (8.0, 55.0, 8.2, 55.2)
    provenance = INSETS.WcsStrategy().fetch(
        _wcs_definition(), bounding_box, 1.0, destination
    )

    assert provenance is not None
    # The opened dataset name carries the substituted key, and the warp
    # receives that OPENED dataset (opened with the request timeout).
    assert any("SECRETKEY" in name for name in opened_names)
    assert captured_inputs[0] == [opened_dataset]

    # The provenance keeps the literal placeholder, never the secret.
    serialized = json.dumps(provenance)
    assert "{api_key}" in serialized
    assert "SECRETKEY" not in serialized


# =====================================================================
# 6. StacCloudOptimizedGeoTiffStrategy -- http_basic kind
#    (GDAL_HTTP_USERPWD passed through the warp-scoped config)
# =====================================================================
class _AuthedSession:
    """A stand-in session carrying HTTP Basic credentials as ``.auth``."""

    def __init__(self, auth):
        self.auth = auth


def _stac_http_basic_definition() -> dict:
    """A ``stac`` definition gated behind http_basic credentials."""
    return {
        "code": "GEOTORGET",
        "access_strategy": "stac",
        "discovery_url_template": "https://example.invalid/stac/search",
        "collections": "nh",
        "search_limit": "50",
        "credential_kind": "http_basic",
        "session_name": "geotorget",
        "native_resolution_m": "1.0",
        "license": "Open data",
        "attribution": "Lantmateriet",
    }


def _stac_geotiff_item() -> dict:
    """One STAC item exposing a single GeoTIFF-typed data asset."""
    return {
        "id": "item-1",
        "assets": {
            "data": {
                "href": "https://example.invalid/cog/tile.tif",
                "type": "image/tiff; application=geotiff",
                "roles": ["data"],
            }
        },
    }


def test_stac_fetch_http_basic_passes_userpwd_config(
    monkeypatch, gdal_stub, tmp_path
):
    """http_basic credentials reach the warp as GDAL_HTTP_USERPWD."""
    monkeypatch.setattr(
        SESSIONS,
        "ensure_session",
        lambda definition, credentials=None: _AuthedSession(("u", "p")),
    )
    monkeypatch.setattr(
        INSETS.StacCloudOptimizedGeoTiffStrategy,
        "discover",
        lambda self, definition, bbox: [_stac_geotiff_item()],
    )

    captured: Dict[str, object] = {}

    def _warp(vsicurl_inputs, *args, **kwargs):
        captured["gdal_config"] = kwargs.get("gdal_configuration_options")
        return True

    monkeypatch.setattr(INSETS, "warp_vsicurl_sources_to_geotiff", _warp)

    destination = str(tmp_path / "out.tif")
    bounding_box = (12.0, 57.0, 12.2, 57.2)
    provenance = INSETS.StacCloudOptimizedGeoTiffStrategy().fetch(
        _stac_http_basic_definition(), bounding_box, 1.0, destination
    )

    assert provenance is not None
    assert captured["gdal_config"] == {"GDAL_HTTP_USERPWD": "u:p"}


def test_stac_fetch_http_basic_login_error_returns_none_no_warp(
    monkeypatch, gdal_stub, tmp_path
):
    """An ensure_session LoginError => None, and warp is never reached."""

    def _ensure_session(definition, credentials=None):
        raise SESSIONS.LoginError(
            "Provider 'GEOTORGET' needs a signed-in session; open Settings."
        )

    monkeypatch.setattr(SESSIONS, "ensure_session", _ensure_session)
    warp_calls: List[tuple] = []
    monkeypatch.setattr(
        INSETS,
        "warp_vsicurl_sources_to_geotiff",
        lambda *a, **k: warp_calls.append(a) or True,
    )
    # discover would raise if reached (it never should be).
    monkeypatch.setattr(
        INSETS.StacCloudOptimizedGeoTiffStrategy,
        "discover",
        lambda self, definition, bbox: (_ for _ in ()).throw(
            AssertionError("discover reached after LoginError")
        ),
    )

    destination = str(tmp_path / "out.tif")
    bounding_box = (12.0, 57.0, 12.2, 57.2)
    result = INSETS.StacCloudOptimizedGeoTiffStrategy().fetch(
        _stac_http_basic_definition(), bounding_box, 1.0, destination
    )
    assert result is None
    assert warp_calls == []


def test_stac_fetch_without_credential_kind_passes_no_config(
    monkeypatch, gdal_stub, tmp_path
):
    """A definition without credential_kind => gdal_configuration_options None."""
    definition = _stac_http_basic_definition()
    definition.pop("credential_kind")
    definition.pop("session_name")

    # ensure_session must NOT be consulted for a plain (session-kind) def.
    def _ensure_session(definition, credentials=None):
        raise AssertionError("ensure_session reached for non-http_basic def")

    monkeypatch.setattr(SESSIONS, "ensure_session", _ensure_session)
    monkeypatch.setattr(
        INSETS.StacCloudOptimizedGeoTiffStrategy,
        "discover",
        lambda self, definition, bbox: [_stac_geotiff_item()],
    )

    captured: Dict[str, object] = {"gdal_config": "unset"}

    def _warp(vsicurl_inputs, *args, **kwargs):
        captured["gdal_config"] = kwargs.get("gdal_configuration_options")
        return True

    monkeypatch.setattr(INSETS, "warp_vsicurl_sources_to_geotiff", _warp)

    destination = str(tmp_path / "out.tif")
    bounding_box = (12.0, 57.0, 12.2, 57.2)
    provenance = INSETS.StacCloudOptimizedGeoTiffStrategy().fetch(
        definition, bounding_box, 1.0, destination
    )

    assert provenance is not None
    assert captured["gdal_config"] is None
