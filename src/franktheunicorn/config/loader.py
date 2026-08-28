"""Load operator and project configs from YAML files on disk.

String values support ``${VAR_NAME}`` env-var expansion.  After
``yaml.safe_load()`` the parsed data is walked recursively and every
``${…}`` reference is replaced with ``os.environ.get(name, "")``.
Partial substitution works too (e.g. ``"${HOME}/frank-data"``).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from django.conf import settings
from pydantic import ValidationError

from franktheunicorn.config.models import OperatorConfig, ProjectConfig

logger = logging.getLogger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _expand_env_vars(data: Any) -> Any:
    """Recursively expand ``${VAR}`` patterns in string values."""
    if isinstance(data, str):
        return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), ""), data)
    if isinstance(data, dict):
        return {k: _expand_env_vars(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_expand_env_vars(item) for item in data]
    return data


def _normalize_project_config(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize legacy/alias project config keys before model parsing."""
    merge_queue = data.get("merge_queue")
    if isinstance(merge_queue, dict):
        mq = dict(merge_queue)
        if "restack" in mq and "restack_enabled" not in mq:
            mq["restack_enabled"] = mq["restack"]
        if mq.get("stale_migration_strategy") == "none" and "delete_stale_migrations" not in mq:
            mq["delete_stale_migrations"] = False
        if (
            mq.get("stale_migration_strategy") == "app-local-diff"
            and "delete_stale_migrations" not in mq
        ):
            mq["delete_stale_migrations"] = True
        data = {**data, "merge_queue": mq}
    return data


def load_operator_config(path: str | Path) -> OperatorConfig:
    """Load operator config from a YAML file. Returns defaults if file doesn't exist.

    Every failure here degrades to :class:`OperatorConfig` defaults, and that is a
    much bigger event than it looks: one bad key anywhere discards the *whole file*
    — every backend, every reviewer entry, every credential, and every setting the
    operator deliberately turned off. The symptom lands on whichever feature stops
    working, with nothing to connect it back to the typo.

    So the failures are logged at ERROR, name the file, name the specific fields,
    and say outright that everything fell back to defaults — the same standard
    CLAUDE.md sets for a gate that stops configured work.

    Note that "defaults" no longer means "everything off": security triage and
    verification default on. That removes the original symptom (reading
    "enabled is false" beside a file saying ``enabled: true``) and adds its mirror
    image, which is why the message says *every* setting rather than naming a
    direction: an operator who switched something off gets it back on.
    """
    p = Path(path)
    try:
        with p.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        data = _expand_env_vars(data)
        if not isinstance(data, dict):
            # Valid YAML, wrong shape: a list, a bare string, a number. Caught here
            # rather than left to ``OperatorConfig(**data)``, which raises TypeError —
            # not one of the exceptions below, so it escaped the whole
            # degrade-to-defaults contract this function documents. `show_config`
            # calls straight into here, so the command written to diagnose a broken
            # config died with a traceback on exactly the configs it exists for.
            logger.error(
                "%s parsed as %s, not a mapping of settings, so EVERY setting is at its "
                "built-in default. The file should be top-level `key: value` pairs — a "
                "leading `- ` on the first line makes the whole document a list.",
                p,
                type(data).__name__,
            )
            return OperatorConfig()
        _warn_unknown_keys(data, p)
        return OperatorConfig(**data)
    except FileNotFoundError:
        logger.debug("Operator config not found at %s, using defaults", p)
        return OperatorConfig()
    except yaml.YAMLError as exc:
        # error(), not exception(): the traceback points at yaml internals and
        # buries the one line that matters, which is what fell back to what.
        logger.error(
            "Could not parse %s as YAML, so EVERY setting in it has been ignored and "
            "the built-in defaults are in force — your backends, reviewer entries and "
            "credentials are all gone, and anything you switched off is back on. %s",
            p,
            exc,
        )
        return OperatorConfig()
    except ValidationError as exc:
        fields = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or '(top level)'}: {err['msg']}"
            for err in exc.errors()[:10]
        )
        logger.error(
            "%s failed validation, so EVERY setting in it has been ignored and the "
            "built-in defaults are in force — your backends, reviewer entries and "
            "credentials are all gone, and anything you switched off is back on. "
            "Fix these and restart: %s",
            p,
            fields,
        )
        return OperatorConfig()


def _warn_unknown_keys(data: dict[str, Any], path: Path) -> None:
    """Name top-level keys the model will silently drop.

    Pydantic's default is ``extra="ignore"``, so a misspelled or misnested block
    vanishes without a word and the feature it configures stays at its default.
    Putting ``verifier:`` at the top level instead of under ``security_triage:``
    is the obvious way to get there, and the only symptom is a feature that
    refuses to turn on.

    A warning rather than a hard error: an older config carrying a key this
    version dropped should still boot.
    """
    known = set(OperatorConfig.model_fields) | {
        field.alias for field in OperatorConfig.model_fields.values() if field.alias
    }
    unknown = sorted(set(data) - known)
    if unknown:
        logger.warning(
            "Ignoring unrecognised top-level key(s) in %s: %s. These configure nothing "
            "— check the spelling and the indentation (a block nested one level too "
            "shallow lands here). Known keys: %s",
            path,
            ", ".join(unknown),
            ", ".join(sorted(known)),
        )


def load_project_configs(directory: str | Path) -> list[ProjectConfig]:
    """Load all project configs from YAML files in a directory.

    An empty *directory* means "not configured", not "here". ``Path("")`` is
    ``Path(".")`` and passes ``is_dir()``, so without this guard an unset
    FRANK_PROJECTS_DIR turned into a scan of the working directory — feeding
    whatever YAML it found (compose.yaml, for one) to ``ProjectConfig`` and
    logging a validation traceback per file. Six call sites pass this value;
    the guard belongs here rather than in whichever ones remembered.
    """
    if not str(directory).strip():
        logger.warning("No project config directory configured; no projects loaded.")
        return []
    d = Path(directory)
    if not d.is_dir():
        return []
    configs: list[ProjectConfig] = []
    for yaml_file in sorted(f for f in d.iterdir() if f.suffix in {".yaml", ".yml"}):
        try:
            with yaml_file.open(encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            data = _expand_env_vars(data)
            data = _normalize_project_config(data)
            configs.append(ProjectConfig(**data))
        except yaml.YAMLError:
            logger.exception("Invalid YAML in project config: %s", yaml_file)
        except ValidationError:
            logger.exception("Validation error in project config: %s", yaml_file)
    return configs


def get_operator_config() -> OperatorConfig:
    """Load operator config from the path configured in Django settings."""
    return load_operator_config(settings.FRANK_OPERATOR_CONFIG)


def get_project_config(name: str) -> ProjectConfig | None:
    """Look up a project config by name.

    Matches against the filename stem convention ("owner-repo") or
    the full name ("owner/repo").

    Returns None if no matching config is found.
    """
    configs = load_project_configs(getattr(settings, "FRANK_PROJECTS_DIR", ""))
    for config in configs:
        if name in (f"{config.owner}-{config.repo}", config.full_name):
            return config
    return None
