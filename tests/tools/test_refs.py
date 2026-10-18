"""Ref formats: pure parsing and formatting, no backend."""
import pytest

from tools import refs

TRACE = "7c1e" + "0" * 28
SPAN = "a91f" + "0" * 12


@pytest.mark.parametrize("ref, kind, parts", [
    (f"span:{TRACE}/{SPAN}", "span", (TRACE, SPAN)),
    ("chunk:refund-policy@v1#3", "chunk", ("refund-policy", "v1", "3")),
    ("doc:refund-policy@v2", "doc", ("refund-policy", "v2")),
    ("log:5b2e0c1a-1111-2222-3333-444455556666", "log", ("5b2e0c1a-1111-2222-3333-444455556666",)),
    ("change:doc-update-2211", "change", ("change:doc-update-2211",)),
    ("prompt:v3", "prompt", ("v3",)),
    ("metric:kestrel_canary_accuracy@2026-10-14T03:00Z", "metric",
     ("kestrel_canary_accuracy", "2026-10-14T03:00Z")),
])
def test_parse_and_format_round_trip(ref, kind, parts):
    parsed = refs.parse_ref(ref)
    assert (parsed.kind, parsed.parts) == (kind, parts)
    assert refs.format_ref(parsed) == ref
    assert str(parsed) == ref


@pytest.mark.parametrize("bad", [
    "", "nonsense", "span:abc", f"span:{TRACE}", "chunk:refund-policy@v1", "chunk:refund-policy#3",
    "doc:refund-policy", "metric:kestrel_canary_accuracy", "metric:kestrel_canary_accuracy@yesterday",
    "table:orders", "log:", "prompt:   ", None, 7,
])
def test_malformed_refs_raise_ref_error(bad):
    with pytest.raises(refs.RefError):
        refs.parse_ref(bad)


def test_builders_match_the_documented_formats():
    assert refs.span_ref("t", "s") == "span:t/s"
    assert refs.chunk_ref("refund-policy@v2#0") == "chunk:refund-policy@v2#0"
    assert refs.doc_ref("refund-policy", "v2") == "doc:refund-policy@v2"
    assert refs.log_ref("abc") == "log:abc"
    assert refs.prompt_ref("v3") == "prompt:v3"


def test_change_ids_are_not_double_prefixed():
    assert refs.change_ref("change:doc-update-2211") == "change:doc-update-2211"
    assert refs.change_ref("doc-update-2211") == "change:doc-update-2211"


def test_metric_timestamps_drop_seconds_only_on_the_minute():
    assert refs.format_ts(1792281600) == "2026-10-18T00:00Z"
    assert refs.format_ts(1792281615) == "2026-10-18T00:00:15Z"
    assert refs.parse_ts("2026-10-18T00:00Z") == refs.parse_ts("2026-10-18T00:00:00+00:00")
    assert refs.parse_ts("2026-10-18T00:00:00") == refs.parse_ts("2026-10-18T00:00:00Z")  # naive means UTC
    # a ref written with seconds normalizes to the same canonical form
    assert str(refs.parse_ref("metric:m@2026-10-18T00:00:00Z")) == "metric:m@2026-10-18T00:00Z"


def test_metric_ref_round_trips_through_the_builder():
    ref = refs.metric_ref("kestrel_canary_accuracy", 1792281615)
    assert refs.parse_ref(ref).parts == ("kestrel_canary_accuracy", "2026-10-18T00:00:15Z")
