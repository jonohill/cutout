from cutout.bench.__main__ import _load_dotenv


def test_missing_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("FOO", raising=False)
    assert _load_dotenv(tmp_path / "nope.env") == 0
    import os

    assert "FOO" not in os.environ


def test_parses_pairs_comments_export_and_quotes(tmp_path, monkeypatch):
    import os

    for k in ("PLAIN", "EXPORTED", "DQUOTED", "SQUOTED"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "\n"
        "PLAIN=bar\n"
        "export EXPORTED=baz\n"
        'DQUOTED="hello world"\n'
        "SQUOTED='single'\n"
        "NOEQUALS\n"
    )

    n = _load_dotenv(env)

    assert n == 4
    assert os.environ["PLAIN"] == "bar"
    assert os.environ["EXPORTED"] == "baz"
    assert os.environ["DQUOTED"] == "hello world"
    assert os.environ["SQUOTED"] == "single"


def test_shell_environment_wins(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("EXISTING", "from-shell")
    env = tmp_path / ".env"
    env.write_text("EXISTING=from-file\n")

    assert _load_dotenv(env) == 0
    assert os.environ["EXISTING"] == "from-shell"
