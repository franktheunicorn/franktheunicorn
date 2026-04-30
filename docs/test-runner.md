# Differential Test Runner

The franktheunicorn worker can run a PR's scoped tests inside a sandboxed
container twice — once on the PR head, once on the base branch with the PR's
new test files cherry-picked on top — and emit a four-way verdict. This
document explains how to enable it for one of your projects.

The full design rationale lives in
[`franktheunicorn-master-design.md` §9](franktheunicorn-master-design.md).
This file is the operator-facing how-to.

## Verdicts

| Verdict | Meaning |
|---------|---------|
| `good`    | Tests pass on the PR, fail on base. The new tests actually validate the change. |
| `suspect` | Tests pass on both. The new tests don't catch the change — likely tautological. |
| `broken`  | Tests fail on the PR (regression), or on both branches (flaky / broken). |
| `infra`   | Base run errored on import / collection / setup. Result is inconclusive. |

## Prerequisites

The worker needs a Docker daemon it can talk to. Two recommended setups:

* **Rootless Docker** on the worker host (preferred for local installs).
* **Docker socket mounted read-only** into the worker container — already
  present in `compose.yaml` (see `docs/franktheunicorn-master-design.md` §9.5).

The web container does **not** get Docker access; only the worker spawns test
containers, and only with `--network=none`, `--read-only`, capability dropped,
`no-new-privileges`, and a writable tmpfs at the workdir.

You also need a local clone of the repo on the worker host. The worker
maintains this automatically under `data/repos/<owner>/<repo>` (see
`worker/repo_manager.py`); no manual setup required.

## Triggering

Once enabled for a project, the runner fires automatically when:

* the PR adds or modifies a test file (detected by diff + PR description NLP),
* **or** the PR is flagged `likely_ai_generated` (mandatory differential).

PRs with no test changes are flagged `no test coverage` instead.

## Enabling — three modes

Pick whichever matches your project. All three live under the `tests:` block
in your project's YAML inside `config/active/projects/`. Exactly one of
`container_image`, `dockerfile`, `auto_build` may be set.

### Mode A — prebuilt image (recommended for upstream projects with a CI image)

```yaml
# config/active/projects/apache-spark.yaml
owner: apache
repo: spark
tests:
  enabled: true
  container_image: ghcr.io/apache/spark-test:latest
  resource_tier: heavy        # 8 CPU, 16GB, 45 min
  test_command: "python -m pytest {tests} --tb=short -q"
```

### Mode B — repo-checked-in Dockerfile (recommended for your own projects)

Commit a `Dockerfile` (anywhere — common: `.frank/Dockerfile`) that produces
an image capable of running your tests, then point the runner at it:

```yaml
# config/active/projects/personal-django.yaml
owner: holdenk
repo: my-app
tests:
  enabled: true
  dockerfile: .frank/Dockerfile
  resource_tier: standard     # 4 CPU, 8GB, 15 min — the default
  workdir: /app
  test_command: "pytest {tests} --tb=short -q"
```

Builds are cached by sha256 of the Dockerfile bytes; a second run with the
same Dockerfile reuses the cached image. Tag pattern:
`franktheunicorn-test/<owner>-<repo>:<hash>`.

### Mode C — auto-build (zero-Dockerfile path)

The runner generates a Dockerfile from your config and builds it the first
time. The hash includes the requirements files, so editing
`requirements-test.txt` triggers a rebuild on the next test run.

```yaml
# config/active/projects/example.yaml
owner: example
repo: hello
tests:
  enabled: true
  resource_tier: light        # 2 CPU, 4GB, 5 min
  auto_build:
    base_image: python:3.12-slim
    requirements_files:
      - requirements.txt
      - requirements-test.txt
    setup_commands:
      - pip install -e .
```

The generated Dockerfile is roughly:

```dockerfile
FROM python:3.12-slim
WORKDIR /workspace
COPY requirements.txt requirements-test.txt /workspace/
RUN pip install --no-cache-dir -r requirements.txt -r requirements-test.txt
RUN pip install -e .
```

## All `tests:` keys

| Key | Default | Notes |
|-----|---------|-------|
| `enabled` | `false` | Master switch. The runner is opt-in. |
| `container_image` | _unset_ | Use a prebuilt image as-is. |
| `dockerfile` | _unset_ | Path inside the repo to a Dockerfile to build. |
| `auto_build` | _unset_ | Block (see Mode C). |
| `resource_tier` | `standard` | One of `light`, `standard`, `heavy`. See §9.4 of the design doc. |
| `test_command` | `python -m pytest {tests} --tb=short -q` | Format string. `{tests}` is replaced with the space-joined test scope. |
| `workdir` | `/workspace` | Where the repo gets bind-mounted (read-only) inside the container. |
| `env` | `{}` | Extra environment variables passed to the container. |

## Resource tiers

| Tier | CPU | Memory | Max runtime |
|------|-----|--------|-------------|
| `light`    | 2 | 4 GB  | 5 min |
| `standard` | 4 | 8 GB  | 15 min |
| `heavy`    | 8 | 16 GB | 45 min |

Bump from `standard` to `heavy` if your test suite genuinely needs it; the
runner kills the container at the timeout boundary.

## Disabling per project

```yaml
tests:
  enabled: false
```

…or just omit the `tests:` block entirely (the default is disabled).

## Troubleshooting

* **Verdict is always `infra`.** Your image is missing test dependencies, or
  the workdir isn't where your tests expect to find the repo. Set `workdir:`
  to match what your test command assumes (e.g. `/app`).
* **`container_image not found` errors.** The runner does not pre-pull
  images. Either pull manually on the worker host, or switch to Mode B/C so
  the image is built locally.
* **Image cache won't invalidate.** Auto-build hashes the Dockerfile text +
  every file listed under `requirements_files`. Files outside that list don't
  bust the cache; either add them to `requirements_files` or `docker rmi
  franktheunicorn-test/<owner>-<repo>:*` to force a rebuild.
* **"PR #N missing base/head SHA".** The poller hasn't finished filling in
  `base_sha`/`head_sha` yet — usually self-corrects on the next poll cycle.
* **"Repo clone unavailable".** Initial clone hasn't finished or failed;
  check `data/repos/<owner>/<repo>` and the worker log.

The `core_testrun` table in `data/frank.sqlite3` records every run with
`container_image`, `differential_verdict`, `results.pr_branch`,
`results.base_cherry_pick`, and `error_log`. It's the first place to look
when a verdict surprises you.

## Security model

See [`security-design.md` §4.1](security-design.md) and the master design
doc §9.5. In short: rootless Docker, `--network=none`, `--read-only` root
filesystem, `cap_drop=ALL`, `no-new-privileges`, repo bind-mounted read-only,
ephemeral tmpfs only at the workdir. The runner never executes PR code on
the host.
