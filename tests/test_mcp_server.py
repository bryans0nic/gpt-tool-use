import json

import pytest

from mcp_server import (
    _clean_json_result,
    _clean_search_result,
    _iter_balanced_json_blocks,
    _read_search_prompt,
    _strip_invalid_json_string_escapes,
    _strip_trailing_json_commas,
)


def test_clean_search_result_strips_citation_markers():
    assert _clean_search_result("Fact one. [1] Fact two.[2]") == "Fact one. Fact two."


def test_strip_trailing_json_commas_removes_trailing_commas():
    assert _strip_trailing_json_commas('{"a": [1, 2,],}') == '{"a": [1, 2]}'


def test_strip_trailing_json_commas_preserves_commas_in_strings():
    assert _strip_trailing_json_commas('{"a": ",]", "b": 1}') == '{"a": ",]", "b": 1}'


def test_strip_invalid_json_string_escapes():
    assert _strip_invalid_json_string_escapes(r'{"a": "\d"}') == '{"a": "d"}'
    assert _strip_invalid_json_string_escapes(r'{"a": "\n \t \" \\"}') == r'{"a": "\n \t \" \\"}'


def test_balanced_blocks_found_after_apostrophe_prose():
    assert list(_iter_balanced_json_blocks('Here\'s the result: {"a": 1} done')) == ['{"a": 1}']


def test_balanced_blocks_ignores_braces_inside_strings():
    assert list(_iter_balanced_json_blocks('{"a": "}"}')) == ['{"a": "}"}']


def test_balanced_blocks_nested():
    assert list(_iter_balanced_json_blocks('x {"a": {"b": [1, 2]}} y')) == ['{"a": {"b": [1, 2]}}']


def test_clean_json_result_plain():
    assert json.loads(_clean_json_result('{"a": 1}')) == {"a": 1}


def test_clean_json_result_fenced_block():
    text = 'Here you go:\n```json\n{"a": 1}\n```\nEnjoy!'
    assert json.loads(_clean_json_result(text)) == {"a": 1}


def test_clean_json_result_trailing_comma():
    assert json.loads(_clean_json_result('{"a": [1, 2,],}')) == {"a": [1, 2]}


def test_clean_json_result_python_literal():
    assert json.loads(_clean_json_result("{'a': True, 'b': None}")) == {"a": True, "b": None}


def test_clean_json_result_embedded_in_prose():
    text = 'Sure! Here\'s what I found: {"funds": [{"ticker": "VTI"}]} Hope that helps.'
    assert json.loads(_clean_json_result(text)) == {"funds": [{"ticker": "VTI"}]}


def test_clean_json_result_rejects_non_json():
    with pytest.raises(ValueError):
        _clean_json_result("I could not find any data, sorry.")


def test_read_search_prompt_requires_exactly_one_source():
    with pytest.raises(ValueError):
        _read_search_prompt(None, None)
    with pytest.raises(ValueError):
        _read_search_prompt("query", "prompt.txt")


def test_read_search_prompt_query_and_file(tmp_path):
    prompt_path = tmp_path / "prompt.txt"
    prompt_path.write_text("file prompt", encoding="utf-8")
    assert _read_search_prompt(None, str(prompt_path)) == "file prompt"
    assert _read_search_prompt("direct", None) == "direct"
