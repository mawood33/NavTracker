import os


def env_setting(name: str, default: str | None = None) -> str:
    """Return an environment variable, or default when it is unset."""

    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def int_setting(name: str, value: str) -> int:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        raise RuntimeError(
            f"Invalid {name}={value!r}. Expected an integer."
        ) from None


def float_setting(name: str, value: str) -> float:
    try:
        return float(value.strip())
    except (TypeError, ValueError):
        raise RuntimeError(
            f"Invalid {name}={value!r}. Expected a number."
        ) from None


def pick_setting(env_name: str, yaml_value: object, default: object) -> object:
    """Environment wins when it is non-blank, then YAML, then default."""

    env_value = os.getenv(env_name)
    if env_value is not None and env_value.strip() != "":
        return env_value.strip()
    if yaml_value is not None:
        return yaml_value
    return default
