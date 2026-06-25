from argparse import Namespace

from datamailer_cli import config


def test_resolve_precedence_flags_over_env_over_file(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text('url = "https://file.example.com"\napi_key = "dm_file"\ndefault_to = "me@file"\n')

    monkeypatch.setenv("DATAMAILER_URL", "https://env.example.com")
    monkeypatch.delenv("DATAMAILER_API_KEY", raising=False)

    args = Namespace(url=None, api_key="dm_flag")
    settings = config.resolve(args, path=cfg)

    assert settings.url == "https://env.example.com"  # env beats file
    assert settings.api_key == "dm_flag"  # flag beats file
    assert settings.default_to == "me@file"  # untouched file value


def test_base_url_strips_trailing_slash(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('url = "https://x.example.com/"\n')
    settings = config.resolve(Namespace(), path=cfg)
    assert settings.base_url == "https://x.example.com"


def test_save_file_roundtrips_and_escapes(tmp_path):
    cfg = tmp_path / "config.toml"
    config.save_file({"url": "https://x", "api_key": 'dm_"quote"'}, path=cfg)
    loaded = config.load_file(cfg)
    assert loaded["url"] == "https://x"
    assert loaded["api_key"] == 'dm_"quote"'


def test_save_file_preserves_unspecified_keys(tmp_path):
    cfg = tmp_path / "config.toml"
    config.save_file({"url": "https://x", "api_key": "dm_a", "default_to": "me@x"}, path=cfg)
    config.save_file({"api_key": "dm_b"}, path=cfg)
    loaded = config.load_file(cfg)
    assert loaded["api_key"] == "dm_b"
    assert loaded["default_to"] == "me@x"
