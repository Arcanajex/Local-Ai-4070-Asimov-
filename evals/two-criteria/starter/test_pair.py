import pytest

from pair import parse_pair


def test_key():
    assert parse_pair("a=b")["key"] == "a"


def test_missing_separator():
    with pytest.raises(ValueError):
        parse_pair("ab")
