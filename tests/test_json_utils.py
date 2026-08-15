from trader.agent.protocol.json_utils import extract_json_object


def test_extract_json_object_first_vs_last() -> None:
    text = '{"a": 1} filler {"b": 2}'
    assert extract_json_object(text, last=False) == {"a": 1}
    assert extract_json_object(text, last=True) == {"b": 2}


def test_extract_json_object_predicate_keeps_matching_object() -> None:
    text = 'x {"kind":"skip"} y {"kind":"keep","n":1} z {"kind":"keep","n":2}'
    assert extract_json_object(text, lambda payload: payload.get("kind") == "keep", last=False) == {
        "kind": "keep",
        "n": 1,
    }
    assert extract_json_object(text, lambda payload: payload.get("kind") == "keep", last=True) == {
        "kind": "keep",
        "n": 2,
    }


def test_extract_json_object_last_returns_top_level_object_without_predicate() -> None:
    payload = {"not": "an envelope"}
    assert extract_json_object('{"not": "an envelope"}', lambda item: "zones" in item, last=True) == payload


def test_extract_json_object_last_does_not_scan_inside_a_top_level_array() -> None:
    assert extract_json_object('[{"zones": {}}]', lambda item: "zones" in item, last=True) is None


def test_extract_json_object_first_recovers_object_despite_trailing_brace() -> None:
    text = 'prose {"symbol": "SPY", "action": "BUY"} }'
    assert extract_json_object(text, last=False) == {"symbol": "SPY", "action": "BUY"}
