"""Tests for server-level validation helpers."""

import pytest

from relay_server.server import _clamp_limit, _parse_session_range, _validate_tags, MAX_LIMIT, MAX_TAGS


def test_clamp_limit_normal():
    assert _clamp_limit(10) == 10
    assert _clamp_limit(1) == 1
    assert _clamp_limit(500) == 500


def test_clamp_limit_too_high():
    assert _clamp_limit(999999) == MAX_LIMIT


def test_clamp_limit_too_low():
    assert _clamp_limit(0) == 1
    assert _clamp_limit(-5) == 1


def test_validate_tags_ok():
    assert _validate_tags(["review:ux", "important"]) is None


def test_validate_tags_too_many():
    tags = [f"tag-{i}" for i in range(MAX_TAGS + 1)]
    err = _validate_tags(tags)
    assert err is not None
    assert "Too many" in err


def test_validate_tags_too_long():
    err = _validate_tags(["x" * 300])
    assert err is not None
    assert "too long" in err.lower()


# --- _parse_session_range tests ---


def test_parse_session_range_single():
    assert _parse_session_range("4", 6) == [3]


def test_parse_session_range_range():
    assert _parse_session_range("4-5", 6) == [3, 4]


def test_parse_session_range_csv():
    assert _parse_session_range("1,3,5", 6) == [0, 2, 4]


def test_parse_session_range_mixed():
    assert _parse_session_range("1,3-5", 6) == [0, 2, 3, 4]


def test_parse_session_range_full():
    assert _parse_session_range("1-6", 6) == [0, 1, 2, 3, 4, 5]


def test_parse_session_range_single_first():
    assert _parse_session_range("1", 1) == [0]


def test_parse_session_range_deduplicates():
    assert _parse_session_range("3,3,3", 5) == [2]


def test_parse_session_range_out_of_range():
    with pytest.raises(ValueError, match="out of range"):
        _parse_session_range("7", 6)


def test_parse_session_range_zero():
    with pytest.raises(ValueError, match="out of range"):
        _parse_session_range("0", 6)


def test_parse_session_range_negative():
    with pytest.raises(ValueError, match="Invalid"):
        _parse_session_range("-1", 6)


def test_parse_session_range_empty():
    with pytest.raises(ValueError):
        _parse_session_range("", 6)


def test_parse_session_range_garbage():
    with pytest.raises(ValueError):
        _parse_session_range("abc", 6)


def test_parse_session_range_reversed_range():
    with pytest.raises(ValueError, match="Invalid range"):
        _parse_session_range("5-3", 6)


def test_fts_query_quotes_punctuated_words():
    from relay_server.server import fts_query

    assert fts_query("relay-mailbox") == '"relay-mailbox"'
    assert fts_query("tail3e79dd.ts.net setup") == '"tail3e79dd.ts.net" setup'
    assert fts_query("sk-live*") == '"sk-live"*'
    assert fts_query("(relay OR nas) NOT tailscale") == "(relay OR nas) NOT tailscale"
    assert fts_query("(relay-mailbox OR nas)") == '("relay-mailbox" OR nas)'
    assert fts_query('"exact phrase" foo-bar') == '"exact phrase" foo-bar'  # user quoting kept as is
    assert fts_query("gigawatt* coupling") == "gigawatt* coupling"
