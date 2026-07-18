"""get_overpass_data's alternative-source switch (2026-07-17).

When a regional extract that was not yet downloaded finishes arriving
while an Overpass request is still stuck in a server's queue, the request
must abandon Overpass and return the extract's bytes instead of waiting
out the queue.  ``get_overpass_data`` grew an optional ``alternative_source``
callable for exactly this; these tests exercise it in isolation.

All headless: the Overpass HTTP path is stubbed (``get_overpass_data``
posts through ``_post_overpass_query_reporting_progress``, which we
replace), back-off sleeping is neutralised, and no network is touched.
"""

import types

import pytest

import O4_OSM_Utils as OSM


# A syntactically complete Overpass answer: status 200, closing </osm>
# tag in the tail, no error/remark trailer, so it passes
# _describe_overpass_response_problem and is returned as-is.
_GOOD_OVERPASS_CONTENT = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<osm version="0.6" generator="test">\n'
    b'<node id="1" lat="0.5" lon="0.5" version="1"/>\n'
    b'</osm>'
)

# What the (late-arriving) regional extract would hand back.
_EXTRACT_CONTENT = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<osm version="0.6" generator="extract">\n'
    b'</osm>'
)


class _FakeResponse:
    """Minimal stand-in for requests.Response as used by the Overpass
    response inspector and the 429 back-off branch."""

    def __init__(self, content, status_code=200, headers=None):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}


@pytest.fixture()
def one_server(monkeypatch):
    """A single pinned Overpass server: no status probing, no network."""
    monkeypatch.setattr(
        OSM,
        "overpass_servers",
        {"alpha": "https://alpha.example/api/interpreter"},
    )
    monkeypatch.setattr(OSM, "overpass_server_choice", "alpha")
    monkeypatch.setattr(OSM.UI, "red_flag", False)
    # Poll at every boundary (the real throttle is time-based) and never
    # actually sleep during back-off.
    monkeypatch.setattr(OSM, "alternative_source_poll_interval_seconds", 0)
    monkeypatch.setattr(OSM.time, "sleep", lambda _seconds: None)


def _make_counting_source(return_values):
    """A zero-argument callable yielding return_values in order, then its
    last element forever; records how many times it was invoked."""
    state = {"calls": 0}

    def source():
        index = state["calls"]
        state["calls"] += 1
        if index < len(return_values):
            return return_values[index]
        return return_values[-1]

    source.state = state
    return source


def test_switches_to_extract_after_two_empty_polls(one_server, monkeypatch):
    """A busy server that never answers (raising as 'too busy') keeps the
    request in the retry/back-off machinery; the alternative source
    returns None twice then the extract bytes, so get_overpass_data leaves
    the Overpass queue and returns those bytes — never an Overpass answer.
    """
    overpass_calls = {"count": 0}

    def busy_server(*_args, **_kwargs):
        overpass_calls["count"] += 1
        raise OSM.requests.RequestException("server too busy")

    monkeypatch.setattr(
        OSM, "_post_overpass_query_reporting_progress", busy_server
    )
    source = _make_counting_source([None, None, _EXTRACT_CONTENT])

    result = OSM.get_overpass_data(
        'way["highway"]', (0, 0, 1, 1), alternative_source=source
    )

    assert result == _EXTRACT_CONTENT
    # The source was polled until it produced bytes.
    assert source.state["calls"] >= 3
    # And no Overpass answer was ever accepted (the server only raised).
    assert overpass_calls["count"] >= 1


def test_always_none_source_lets_overpass_proceed(one_server, monkeypatch):
    """When the alternative source never has anything, the ordinary
    Overpass path runs and its successful answer is returned."""
    def good_server(*_args, **_kwargs):
        return _FakeResponse(_GOOD_OVERPASS_CONTENT)

    monkeypatch.setattr(
        OSM, "_post_overpass_query_reporting_progress", good_server
    )
    source = _make_counting_source([None])

    result = OSM.get_overpass_data(
        'way["highway"]', (0, 0, 1, 1), alternative_source=source
    )

    assert result == _GOOD_OVERPASS_CONTENT
    # It was consulted at least once (between-tentatives boundary) but
    # never produced anything, so Overpass won.
    assert source.state["calls"] >= 1


def test_raising_source_is_swallowed_and_overpass_proceeds(
    one_server, monkeypatch
):
    """An alternative source that raises must be treated as 'nothing yet',
    never propagated — the Overpass fallback must survive a broken
    alternative source."""
    def exploding_source():
        raise RuntimeError("extract backend blew up")

    def good_server(*_args, **_kwargs):
        return _FakeResponse(_GOOD_OVERPASS_CONTENT)

    monkeypatch.setattr(
        OSM, "_post_overpass_query_reporting_progress", good_server
    )

    result = OSM.get_overpass_data(
        'way["highway"]', (0, 0, 1, 1), alternative_source=exploding_source
    )

    assert result == _GOOD_OVERPASS_CONTENT


def test_no_alternative_source_never_touches_the_poller(
    one_server, monkeypatch
):
    """With no alternative source the code path is the original: the
    polling helper is never invoked."""
    poll_calls = {"count": 0}

    def spy_poll(alternative_source, poll_state):
        poll_calls["count"] += 1
        return None

    monkeypatch.setattr(OSM, "_alternative_source_bytes", spy_poll)

    def good_server(*_args, **_kwargs):
        return _FakeResponse(_GOOD_OVERPASS_CONTENT)

    monkeypatch.setattr(
        OSM, "_post_overpass_query_reporting_progress", good_server
    )

    result = OSM.get_overpass_data('way["highway"]', (0, 0, 1, 1))

    assert result == _GOOD_OVERPASS_CONTENT
    assert poll_calls["count"] == 0


def test_switch_inside_the_server_working_wait(one_server, monkeypatch):
    """The live case: the switch happens inside a server's own
    working-on-it wait loop (_post_overpass_query_reporting_progress),
    not only between tentatives.  A POST that never returns keeps the
    join loop spinning; the alternative source yields bytes on its second
    look and the helper returns them, abandoning the pending request."""
    monkeypatch.setattr(OSM, "progress_update_interval_seconds", 0.01)

    release = types.SimpleNamespace(stop=None)

    class _HangingSession:
        def post(self, *_args, **_kwargs):
            # Block until the test releases us, so the join loop keeps
            # spinning and polling the alternative source meanwhile.  The
            # bounded wait guarantees the daemon thread cannot outlive the
            # test process.
            import threading

            release.stop = threading.Event()
            release.stop.wait(timeout=5)
            return _FakeResponse(_GOOD_OVERPASS_CONTENT)

    monkeypatch.setattr(OSM, "_get_http_session", lambda: _HangingSession())

    source = _make_counting_source([None, _EXTRACT_CONTENT])
    poll_state = [None]

    result = OSM._post_overpass_query_reporting_progress(
        "alpha",
        "[out:xml];out;",
        "",
        alternative_source=source,
        poll_state=poll_state,
    )

    if release.stop is not None:
        release.stop.set()

    assert result == _EXTRACT_CONTENT
    assert source.state["calls"] >= 2
