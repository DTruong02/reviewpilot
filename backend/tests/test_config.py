from app.core.config import PROJECT_ROOT, Settings


def test_blank_example_credentials_load_as_unset_defaults(monkeypatch):
    for name in (
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=PROJECT_ROOT / ".env.example")

    assert settings.github_app_id is None
    assert settings.github_app_private_key_path is None
    assert settings.openai_api_key == ""
    assert settings.openai_model == ""
