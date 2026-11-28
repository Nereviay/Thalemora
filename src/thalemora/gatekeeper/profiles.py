"""存量进程内模型配置协议。读写已有文件，路径由应用提供。"""
import copy
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

PROFILE_NAMES = ("writer", "daily", "weekly", "monthly", "gatekeeper")
REASONING_EFFORTS = ("low", "medium", "high")
MODEL_MIN_LENGTH = 1
MODEL_MAX_LENGTH = 128
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")


class ModelConfigError(ValueError):
    """The on-disk or submitted model configuration is invalid."""


def _validate_profile(name, profile):
    if not isinstance(profile, dict):
        raise ModelConfigError(f"model config profile {name!r} must be an object")
    expected = {"model", "thinking_enabled", "reasoning_effort"}
    if set(profile) != expected:
        raise ModelConfigError(
            f"model config profile {name!r} fields must be {sorted(expected)}"
        )
    model = profile["model"]
    if not isinstance(model, str):
        raise ModelConfigError(f"model config profile {name!r} model must be a string")
    if not MODEL_MIN_LENGTH <= len(model) <= MODEL_MAX_LENGTH:
        raise ModelConfigError(
            f"model config profile {name!r} model length must be 1..{MODEL_MAX_LENGTH}"
        )
    if not MODEL_RE.fullmatch(model):
        raise ModelConfigError(
            f"model config profile {name!r} model contains invalid characters"
        )
    thinking_enabled = profile["thinking_enabled"]
    if type(thinking_enabled) is not bool:
        raise ModelConfigError(
            f"model config profile {name!r} thinking_enabled must be boolean"
        )
    reasoning_effort = profile["reasoning_effort"]
    if reasoning_effort not in REASONING_EFFORTS:
        raise ModelConfigError(
            f"model config profile {name!r} reasoning_effort must be one of "
            f"{', '.join(REASONING_EFFORTS)}"
        )
    return {
        "model": model,
        "thinking_enabled": thinking_enabled,
        "reasoning_effort": reasoning_effort,
    }


def validate_config(value):
    if not isinstance(value, dict):
        raise ModelConfigError("model config must be an object")
    if set(value) != set(PROFILE_NAMES):
        raise ModelConfigError(
            f"model config profiles must be {', '.join(PROFILE_NAMES)}"
        )
    return {name: _validate_profile(name, value[name]) for name in PROFILE_NAMES}


def load_config(path, *, defaults=None):
    path = Path(path)
    if not path.exists():
        if defaults is None:
            raise ModelConfigError("configure model profiles before use")
        return validate_config(defaults)
    try:
        with path.open("r", encoding="utf-8") as config_file:
            value = json.load(config_file)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelConfigError(f"cannot read valid model config {path}: {exc}") from exc
    return validate_config(value)


def save_config(value, path):
    validated = validate_config(value)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as config_file:
            json.dump(validated, config_file, ensure_ascii=False, indent=2)
            config_file.write("\n")
            config_file.flush()
            os.fsync(config_file.fileno())
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return copy.deepcopy(validated)


def get_profile(name, path, *, defaults=None):
    if name not in PROFILE_NAMES:
        raise ModelConfigError(f"unknown model config profile: {name}")
    return load_config(path, defaults=defaults)[name]


def profile_fingerprint(profile):
    validated = _validate_profile("fingerprint", profile)
    effective = {
        "model": validated["model"],
        "thinking_enabled": validated["thinking_enabled"],
        "reasoning_effort": (
            validated["reasoning_effort"]
            if validated["thinking_enabled"] else None
        ),
    }
    encoded = json.dumps(
        effective, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def request_profile(name, path, *, defaults=None):
    """Load a fresh profile and translate it into DeepSeek request fields."""
    profile = get_profile(name, path, defaults=defaults)
    return profile, {
        "model": profile["model"],
        "thinking": {
            "type": "enabled" if profile["thinking_enabled"] else "disabled"
        },
        "reasoning_effort": profile["reasoning_effort"],
    }


def public_payload(config, *, defaults=None):
    return {
        "provider": "DeepSeek",
        "config": copy.deepcopy(config),
        "defaults": validate_config(defaults) if defaults is not None else None,
        "options": {"reasoning_effort": list(REASONING_EFFORTS)},
    }
