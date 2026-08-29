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
import time
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
        logger.warning("Project config directory %s does not exist; no projects loaded.", d)
        return []
    fingerprint = _fingerprint(d)
    cached = _config_cache.get(str(d))
    if cached is not None and fingerprint is not None and cached[0] == fingerprint:
        return list(cached[1])
    configs: list[ProjectConfig] = []
    rejected: list[str] = []
    for yaml_file in sorted(f for f in d.iterdir() if f.suffix in {".yaml", ".yml"}):
        try:
            with yaml_file.open(encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            data = _expand_env_vars(data)
            if not isinstance(data, dict):
                # Valid YAML, wrong shape. A leading ``- `` makes the whole
                # document a list, which dies in _normalize_project_config with
                # an AttributeError — not one of the exceptions below, so it took
                # out every *other* project config in the directory with it.
                rejected.append(
                    f"{yaml_file.name}: parsed as {type(data).__name__}, not a mapping of "
                    "settings (a leading '- ' makes the whole document a list)"
                )
                continue
            data = _normalize_project_config(data)
            configs.append(ProjectConfig(**data))
        except yaml.YAMLError as exc:
            rejected.append(f"{yaml_file.name}: not valid YAML ({str(exc).splitlines()[0]})")
        except ValidationError as exc:
            rejected.append(f"{yaml_file.name}: {_validation_fields(exc)}")
        except Exception as exc:
            # One bad file must never take out the others, and since CoreConfig.ready
            # calls this, an escape here aborts app load — `manage.py check`, migrate,
            # runserver and the worker all die on a traceback. Three real ways to get
            # here, none of them a YAMLError or a ValidationError: a non-string key
            # (`1: oops`) is a TypeError out of ``ProjectConfig(**data)``, a latin-1
            # byte is a UnicodeDecodeError, and mode 000 is a PermissionError.
            rejected.append(f"{yaml_file.name}: {type(exc).__name__}: {exc}")
    _log_project_summary(d, configs, rejected)
    if fingerprint is not None and _has_settled(fingerprint):
        _config_cache[str(d)] = (fingerprint, configs)
    else:
        # Never cache a just-written directory: see CACHE_SETTLE_SECONDS.
        _config_cache.pop(str(d), None)
    return list(configs)


#: Directory -> ``(fingerprint, configs)``. The nav context processor reads
#: project config on every page render and ``index()`` reads it three more times,
#: so uncached this was four directory walks and four pydantic passes per page.
#: Invalidated by file mtime and size rather than a TTL, so an operator editing a
#: config is picked up on the next read with no restart — which is also how the
#: worker sees a project added mid-run.
_config_cache: dict[str, tuple[tuple[tuple[str, int, int], ...] | None, list[ProjectConfig]]] = {}


#: How old the newest config file must be before the directory is cached.
#:
#: Filesystem mtime granularity is much coarser than nanoseconds — measured at
#: ~10 ms on this box, and 1 s on ext3/HFS+ — so a *same-size* edit inside one
#: tick leaves ``st_mtime_ns`` unchanged and the fingerprint sees nothing. A typo
#: fix of equal length, a case change, or a scripted write-then-read all land
#: there. Waiting for the file to settle closes the window, and the cost is
#: re-reading a directory that just changed, which is when you want a re-read.
CACHE_SETTLE_SECONDS = 2.0


def _has_settled(fingerprint: tuple[tuple[str, int, int], ...]) -> bool:
    if not fingerprint:
        return True
    newest = max(mtime for _name, mtime, _size in fingerprint)
    return time.time_ns() - newest > CACHE_SETTLE_SECONDS * 1e9


def _fingerprint(d: Path) -> tuple[tuple[str, int, int], ...] | None:
    """Name, mtime and size of every YAML file in *d*. None if it can't be read."""
    try:
        out: list[tuple[str, int, int]] = []
        for f in sorted(d.iterdir()):
            if f.suffix in {".yaml", ".yml"}:
                st = f.stat()
                out.append((f.name, st.st_mtime_ns, st.st_size))
        return tuple(out)
    except OSError:
        return None


def _validation_fields(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc']) or '(top level)'}: {err['msg']}"
        for err in exc.errors()[:10]
    )


#: Last ``(directory, loaded, rejected)`` triple logged. Six call sites read
#: project config and one of them is the nav context processor, so an
#: unconditional summary is a log line per page view. Re-logs when a file is
#: added, fixed, or newly broken.
_last_summary: tuple[str, tuple[str, ...], tuple[str, ...]] | None = None


def _log_project_summary(
    directory: Path, configs: list[ProjectConfig], rejected: list[str]
) -> None:
    """Name what loaded and what was thrown out.

    A rejected file used to log a pydantic traceback and nothing else, so the
    only way to notice a typo'd project was that its repo quietly behaved as
    unconfigured — no dashboard row, no nav entry, no polling.
    """
    global _last_summary
    loaded = tuple(f"{c.owner}/{c.repo}{'' if c.enabled else ' (disabled)'}" for c in configs)
    summary = (str(directory), loaded, tuple(rejected))
    if summary == _last_summary:
        return
    _last_summary = summary
    logger.info(
        "Loaded %d project config(s) from %s: %s",
        len(configs),
        directory,
        ", ".join(loaded) or "(none)",
    )
    if rejected:
        logger.warning(
            "Ignored %d file(s) in %s: %s. These configure nothing — their repos are "
            "unconfigured, so they get no dashboard row, no nav entry and no polling.",
            len(rejected),
            directory,
            "; ".join(rejected),
        )


def get_operator_config() -> OperatorConfig:
    """Load operator config from the path configured in Django settings."""
    return load_operator_config(settings.FRANK_OPERATOR_CONFIG)


def get_project_config(name: str) -> ProjectConfig | None:
    """Look up a project config by name.

    Matches against the filename stem convention ("owner-repo") or
    the full name ("owner/repo"), case-insensitively — GitHub's search results
    and a pasted PR URL don't promise the same casing as the YAML file, and
    the allow-list this feeds (``configured_owner_repos``, ``Project.configured_q``)
    already matches case-insensitively. A mismatch here means a configured
    project's own casing variant looks unconfigured and gets a stray disabled
    ``Project`` row.

    Returns None if no matching config is found.
    """
    configs = load_project_configs(getattr(settings, "FRANK_PROJECTS_DIR", ""))
    lowered = name.lower()
    for config in configs:
        if lowered in (f"{config.owner}-{config.repo}".lower(), config.full_name.lower()):
            return config
    return None


def configured_owner_repos() -> frozenset[tuple[str, str]]:
    """``(owner, repo)`` pairs from enabled project YAML.

    This is the allow-list. A GitHub search will happily return every repo the
    operator has ever touched; those are not projects we monitor unless there
    is a file in the projects directory saying so.
    """
    configs = load_project_configs(getattr(settings, "FRANK_PROJECTS_DIR", ""))
    return frozenset((c.owner, c.repo) for c in configs if c.enabled)
