# Asset Attribution

All visual assets in this project are released under CC0 1.0 Universal
(Public Domain Dedication) by their respective creators via
[Openclipart](https://openclipart.org/).

## Dashboard SVG Illustrations

| File | Title | Artist | Source | License |
|------|-------|--------|--------|---------|
| `static/dashboard/img/rubber-duck.svg` | Rubber Duck | PrinterKiller | [Openclipart #159499](https://openclipart.org/detail/159499/rubber-duck) | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) |
| `static/dashboard/img/bathtub.svg` | Bath tub | Moini | [Openclipart #151903](https://openclipart.org/detail/151903/bath-tub) | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) |
| `static/dashboard/img/duck-icon.svg` | Duck | Manuela (via Noun Project) | [Openclipart #348177](https://openclipart.org/detail/348177/duck) | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) |

While CC0 does not require attribution, we credit the artists because
it's the right thing to do.

## Code and Text

| Origin | What | Where in this repo | Revision | License |
|--------|------|--------------------|----------|---------|
| [Apache Magpie](https://github.com/apache/magpie) (The Apache Software Foundation) | Logic ported into frank's own modules, each naming its upstream file and commit in its docstring: the security-disclosure phrase lists and scan (`security/fix_language.py`, `review/checks/security_disclosure.py`), mention escaping (`backends/mentions.py`), the post-time moved-head guard, inline-anchor fold and verify-never-retry rule (`backends/poster.py`), the untrusted-content rule (`core/untrusted.py`), the ASF forwarder-relay adapter data (`data_access/email_inbox/parser.py`), the triage disposition rubric (`security/prompt.py`), the CVE record merge guards (`security/cve_merge.py`), the read-only reviewer flags (`config/models.py`), the process-group agent runner (`review/tool_executor.py`), the git-command gate (`.claude/hooks/git_gate.py`), the maintainer-court and author-association signals (`scoring/signals.py`), the approval-withdrawal rule (`worker/merge_intent.py`) and the final-release tag rule (`security/release_tags.py`). Skill text may be vendored as prompt material and Python tools pinned by commit; none are yet | `src/franktheunicorn/magpie/vendored/` (each file opens with a provenance header naming its upstream path and commit); tools are pinned in `src/franktheunicorn/magpie/magpie-pin.yaml`; ports are listed in `docs/magpie-integration.md` section 4 | The commit in `magpie-pin.yaml` | [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0) |

Apache-2.0 asks that redistributed source keep its notices. Every vendored
file keeps Magpie's own SPDX header in its body, and the provenance header
above it names the exact upstream path and commit so the copy can be diffed
against its source. `manage.py magpie_sync --check` verifies the copies match
the lock; see `docs/magpie-integration.md` for what is borrowed and why.
