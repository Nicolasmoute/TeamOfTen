from pathlib import Path


def test_materialize_system_prompt_uses_file_and_cleans_up() -> None:
    from server.runtimes.claude import (
        _cleanup_materialized_system_prompt,
        _materialize_system_prompt,
    )

    prompt = "global rules\n\n" + ("handoff detail\n" * 5000)
    option, path = _materialize_system_prompt(prompt)

    assert isinstance(option, dict)
    assert option["type"] == "file"
    assert path is not None
    assert Path(option["path"]) == path
    assert path.read_text(encoding="utf-8") == prompt

    _cleanup_materialized_system_prompt(path)
    assert not path.exists()


def test_materialize_empty_system_prompt_stays_inline() -> None:
    from server.runtimes.claude import _materialize_system_prompt

    option, path = _materialize_system_prompt("")

    assert option == ""
    assert path is None


def test_materialize_system_prompt_uses_unique_paths() -> None:
    from server.runtimes.claude import (
        _cleanup_materialized_system_prompt,
        _materialize_system_prompt,
    )

    _option_a, path_a = _materialize_system_prompt("prompt a")
    _option_b, path_b = _materialize_system_prompt("prompt b")

    try:
        assert path_a is not None
        assert path_b is not None
        assert path_a != path_b
        assert path_a.read_text(encoding="utf-8") == "prompt a"
        assert path_b.read_text(encoding="utf-8") == "prompt b"
    finally:
        _cleanup_materialized_system_prompt(path_a)
        _cleanup_materialized_system_prompt(path_b)
