"""``.env`` loading does not override variables already in the environment."""

import os

from navtracker_common.envfile import load_env_file


def test_load_env_file_fills_missing_keys_only(monkeypatch, tmp_path):
    monkeypatch.setenv("KEEP", "set")
    path = tmp_path / ".env"
    path.write_text(
        "\n".join(
            [
                "# comment",
                "KEEP=no",
                "NEW=value",
                'export QUOTED="a b"',
                "BLANK=",
            ]
        ),
        encoding="utf-8",
    )
    load_env_file(path)
    assert os.environ["KEEP"] == "set"
    assert os.environ["NEW"] == "value"
    assert os.environ["QUOTED"] == "a b"
    assert os.environ["BLANK"] == ""
