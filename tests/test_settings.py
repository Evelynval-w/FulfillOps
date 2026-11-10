import pytest

from fulfillops.common.settings import ConfigError, load_settings

BASE = {"DATABASE_URL": "postgresql://u:p@db:5432/x"}


def test_defaults_are_safe():
    s = load_settings(BASE)
    assert s.pool_size == 20
    assert s.port == 8080
    assert s.app_env == "production"  # production unless explicitly told otherwise
    assert s.fail_readiness is False
    assert s.is_test is False


def test_values_are_read_from_the_environment():
    s = load_settings(
        {**BASE, "POOL_SIZE": "5", "PORT": "9000", "APP_ENV": "test", "FAIL_READINESS": "true"}
    )
    assert (s.pool_size, s.port, s.fail_readiness, s.is_test) == (5, 9000, True, True)


def test_database_url_is_required():
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        load_settings({})


@pytest.mark.parametrize(
    "name,value",
    [
        ("POOL_SIZE", "abc"),
        ("POOL_SIZE", "0"),
        ("PORT", "-1"),
        ("FAIL_READINESS", "maybe"),
        ("APP_ENV", "staging"),
    ],
)
def test_invalid_values_are_rejected(name, value):
    with pytest.raises(ConfigError, match=name):
        load_settings({**BASE, name: value})


def test_error_message_never_contains_the_secret():
    with pytest.raises(ConfigError) as err:
        load_settings({"DATABASE_URL": "postgresql://u:supersecret@db/x", "POOL_SIZE": "abc"})
    assert "supersecret" not in str(err.value)
