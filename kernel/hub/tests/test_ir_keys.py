"""SQL quoting must preserve simple shuffle keys without claiming expression support."""

import pytest

from hub.ir import parse_group_keys, parse_sort_keys


def test_quoted_simple_columns_keep_the_same_distributed_keys():
    assert parse_group_keys('"category", "user_id"') == parse_group_keys("category, user_id")
    assert parse_sort_keys('"score" DESC, "id" ASC') == parse_sort_keys("score DESC, id ASC")
    assert parse_group_keys('"ALL"') == ["ALL"]
    assert parse_sort_keys('"NULL" DESC') == [("NULL", True)]


@pytest.mark.parametrize("expression", [
    "ALL", "NULL", "TRUE", "FALSE", "CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP",
    "LOCALTIME", "LOCALTIMESTAMP", "CURRENT_USER", "SESSION_USER", "CURRENT_SCHEMA",
    "CURRENT_CATALOG", "CURRENT_ROLE", "USER",
])
def test_bare_expressions_are_not_mistaken_for_shuffle_columns(expression):
    assert parse_group_keys(expression) is None
    assert parse_sort_keys(expression) is None
    assert parse_sort_keys(f"{expression} DESC") is None


@pytest.mark.parametrize("fragment", [
    '"team, label"', '"cost ""USD"""', '"a.b"', '"two words"',
    '"İ"', '"K"', "İ", "K", "lower(name)", "1", "a,", "a,,b", "a + b",
])
def test_complex_keys_keep_the_existing_local_fallback(fragment):
    assert parse_group_keys(fragment) is None
    assert parse_sort_keys(fragment) is None


def test_explicit_null_ordering_still_requires_the_local_engine():
    assert parse_sort_keys('"score" DESC NULLS LAST') is None
    assert parse_sort_keys("score ASC NULLS FIRST") is None
