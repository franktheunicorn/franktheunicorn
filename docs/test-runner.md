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
  container_image: ghcr.io/apache/spark-test:latest  # illustrative: Spark publishes no runnable test image
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
| `test_globs` | `[]` | fnmatch globs; only identified tests matching one reach `test_command`. |
| `build_contexts` | `{}` | Named contexts for `dockerfile` (`COPY --from=<name>`), repo-relative. Builds with `docker buildx`. |
| `build_command` | _unset_ | Runs in the checkout before the tests, offline. See below. |
| `warm_command` | `build_command` | Fills the dependency cache from base code, with network. |
| `warm_key_files` | `[]` | Files/dirs whose content keys the cache; re-warm when they change. |
| `offline_env` | `{}` | Env for the offline build/test container only (e.g. `COURSIER_MODE: offline`). |
| `build_timeout_seconds` | `5400` | Added to the tier's timeout when there is a build. |

## Projects that must build before they test

PySpark tests need Spark's jars, so the checkout has to be built first. PR
code still never gets network:

1. **Warm** (base code, network on): `warm_command` runs in a checkout of the
   PR's base and of its fork point with master, with `$HOME` bound to
   `data/test-cache/<owner>-<repo>/`. Skipped while `warm_key_files` are
   unchanged since a previous warm. The fork point matters: a PR branched
   before a dependency bump builds against the old version.
2. **Build + test** (PR code, `--network=none`): the cache is mounted
   read-only, copied into a per-run `$HOME`, then `build_command` and
   `test_command` run with `offline_env`. A PR that adds a dependency the
   cache doesn't have fails the build, and a failed build is an `infra`
   verdict, never a test result.

The workspace is writable for builds, and these containers run as the
worker's uid, so build output on a rootful daemon stays cleanable.

```yaml
# Apache Spark — what this machine runs
tests:
  enabled: true
  dockerfile: dev/spark-test-image/python-312/Dockerfile
  build_contexts: {root: "."}
  resource_tier: heavy
  test_globs: ["python/pyspark/*test_*.py"]
  warm_key_files: ["pom.xml", "project", "build"]
  # build/sbt downloads its own launcher, so the warm stashes it in $HOME.
  warm_command: "build/sbt -Phive package && cp build/sbt-launch-*.jar $HOME/"
  build_command: "cp $HOME/sbt-launch-*.jar build/ && build/sbt -Phive package"
  offline_env: {COURSIER_MODE: offline, SBT_OPTS: "-Dsbt.offline=true"}
  env:
    JAVA_TOOL_OPTIONS: "-Duser.home=/frank-home -Djava.io.tmpdir=/frank-home/tmp"
    SPARK_HOME: /workspace
    SPARK_TESTING: "1"
    SPARK_LOCAL_IP: 127.0.0.1
  # The image has no pytest (Spark's suites are unittest); paths become modules.
  test_command: >-
    mkdir -p $HOME/tmp &&
    export PYTHONPATH=/workspace/python:$(ls /workspace/python/lib/py4j-*-src.zip) &&
    python -m unittest -v $(for t in {tests}; do echo "$t" |
    sed -e 's#^python/##' -e 's#\.py$##' -e 's#/#.#g'; done)
```

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
the host. With `build_command` the workspace is writable and the dependency
cache is mounted read-only; only the base-code warm gets network.
