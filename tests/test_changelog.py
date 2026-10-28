# SPDX-License-Identifier: MIT

"""CHANGELOG records 0.1.14, 0.1.13, 0.1.12, 0.1.11, 0.1.10, 0.1.9, 0.1.8, 0.1.7, 0.1.6, 0.1.5, 0.1.4, 0.1.3, and 0.1.1 and still has no 0.1.0 section."""

import unittest
from pathlib import Path

TEXT = (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text()
PROSE = " ".join(TEXT.split())


class ChangelogTests(unittest.TestCase):
    def test_format_and_unreleased_section(self):
        self.assertIn("The format is Keep a Changelog.", TEXT)
        self.assertIn("## [Unreleased]", TEXT)
        self.assertIn(
            "A poll pass keeps going when an older claimed run has no readable snapshot.",
            PROSE,
        )
        self.assertIn("That run is skipped and its submission stays.", PROSE)
        self.assertIn("The source distribution omits `tests` and `scripts`.", PROSE)
        self.assertIn("without a header line in `checks.py`.", PROSE)
        self.assertIn("MIT License", PROSE)
        self.assertIn(
            "A host copy of the worker unit must set `NoNewPrivileges=yes`. "
            "The repository unit sets that directive. "
            "A copy that omits it is reported by systemd as "
            "`NoNewPrivileges=no` and must be set to `yes`. "
            "This note does not edit a running unit.",
            PROSE,
        )
        self.assertIn("A pull request that changes a workflow file is refused.", PROSE)
        self.assertIn(
            "A host that upgrades onto a tree where the job network defaults to `none`",
            PROSE,
        )
        self.assertIn("Cargo and pip need it.", PROSE)
        self.assertIn("The repository unit omits the flag.", PROSE)
        self.assertIn("Job containers use Docker network `none` by default.", PROSE)
        self.assertIn("`worker --network bridge` opts in to egress.", PROSE)
        self.assertIn("Service containers start only with that opt-in.", PROSE)
        self.assertIn("Every engine call uses the Docker context named `default`", PROSE)
        self.assertIn("An inherited `DOCKER_CONTEXT` or `DOCKER_HOST` does not select", PROSE)
        self.assertIn("The scheduler thread uses that same context.", PROSE)
        self.assertIn("A bash or sh `run` script quotes each `${{ }}` value.", PROSE)
        self.assertIn("An expression in `eval` or in a shell `-c` command is refused.", PROSE)
        self.assertIn("A brace group or an `if`, `while`, or `for` body does not hide it.", PROSE)
        self.assertIn("A backslash in that command name does not hide it either.", PROSE)
        self.assertIn("A backslash before a newline is not part of that name.", PROSE)
        self.assertIn(
            "`$'eval'`, `$\"eval\"`, and a command substitution used as the command", PROSE
        )
        self.assertIn("An unquoted `*`, `?`, or `[` in that command name is refused", PROSE)
        self.assertIn("The `[` command stays accepted.", PROSE)
        self.assertIn("An unquoted `{` in that command name is refused", PROSE)
        self.assertIn("`builtin eval` is refused the same way.", PROSE)
        self.assertIn(
            "A missing or unrecognized pull request author association is refused.", PROSE
        )
        self.assertIn("its first parent is the listed base", PROSE)

    def test_0_1_5_records_the_same_repository_pull_request_rule(self):
        self.assertIn("## [0.1.5] - 2026-10-17", TEXT)
        section = TEXT.split("## [0.1.5] - 2026-10-17", 1)[1].split("## [0.1.4]", 1)[0]
        prose = " ".join(section.split())
        self.assertIn(
            "A same-repository pull request with a listed association goes on "
            "to the per-commit write check",
            prose,
        )
        self.assertIn(
            "A fork, a missing association, and an unrecognized association are still refused.",
            prose,
        )
        self.assertIn("GitHub reports the type as Bot", prose)
        self.assertIn("GitHub committed and signed it as web-flow.", prose)
        self.assertIn(
            "`--allow-pr-workflow` still accepts only OWNER, MEMBER, and COLLABORATOR.",
            prose,
        )
        self.assertIn("The package version is 0.1.5.", prose)
        self.assertIn("This change does not create a tag.", prose)
        self.assertNotIn("A step `continue-on-error` may be true", prose)
        self.assertNotIn("A poll can record the pending status it already posted", prose)

    def test_0_1_14_is_the_current_version_and_unreleased_is_empty(self):
        self.assertIn("The current package version is 0.1.14.", TEXT)
        self.assertIn("## [0.1.14] - 2026-10-27", TEXT)
        section = TEXT.split("## [0.1.14] - 2026-10-27", 1)[1].split("## [0.1.13]", 1)[0]
        prose = " ".join(section.split())
        self.assertIn("The package version is 0.1.14.", prose)
        self.assertIn("This change does not create a tag.", prose)
        self.assertIn("## [Unreleased]\n\n## [0.1.14] - 2026-10-27", TEXT)

    def test_0_1_14_records_the_poll_and_reconcile_changes(self):
        section = TEXT.split("## [0.1.14] - 2026-10-27", 1)[1].split("## [0.1.13]", 1)[0]
        prose = " ".join(section.split())
        self.assertIn("records an error for one run and carries on with the other runs", prose)
        self.assertIn("Final commit-status posts are retried with backoff", prose)
        self.assertIn("`reconcile` subcommand", prose)

    def test_0_1_13_is_the_previous_release_and_records_the_refusal_reason(self):
        section = TEXT.split("## [0.1.13] - 2026-10-27", 1)[1].split("## [0.1.12]", 1)[0]
        prose = " ".join(section.split())
        self.assertIn("App-key commit status carries its refusal reason as the description", prose)

    def test_0_1_13_records_the_refusal_status_reason(self):
        section = TEXT.split("## [0.1.13] - 2026-10-27", 1)[1].split("## [0.1.12]", 1)[0]
        prose = " ".join(section.split())
        self.assertIn("App-key commit status carries its refusal reason as the description", prose)

    def test_0_1_12_records_the_refusal_and_dashboard_fixes(self):
        section = TEXT.split("## [0.1.12] - 2026-10-26", 1)[1].split("## [0.1.11]", 1)[0]
        prose = " ".join(section.split())
        self.assertIn("Refused pull request runs post their refusal status before a worker", prose)
        self.assertIn("stored run logs display as terminal data", prose)
        self.assertIn("carriage returns and control bytes no longer drive the terminal", prose)

    def test_0_1_11_records_the_hardening_batch(self):
        section = TEXT.split("## [0.1.11] - 2026-10-19", 1)[1].split("## [0.1.10]", 1)[0]
        prose = " ".join(section.split())
        self.assertIn("enforces the per-job disk budget during the run", prose)
        self.assertIn("A submission that would exceed the budget is refused", prose)
        self.assertIn("The breach is latched on the lease", prose)
        self.assertIn("fails closed when its audit record cannot be written", prose)
        self.assertIn("without the token.", prose)
        self.assertIn("comes only from the authenticated actor, never the commit author", prose)
        self.assertIn("withholds actor eligibility", prose)
        self.assertIn("Passwordless sudo still drops every capability.", prose)
        self.assertIn("`DAC_READ_SEARCH` is no longer kept", prose)
        self.assertIn("the apt, unlink, and workspace flows from 0.1.7 still work", prose)
        self.assertIn("adds no capabilities.", prose)
        self.assertIn("A low-severity hardening batch is included", prose)
        self.assertIn("an empty or missing run database fails closed", prose)
        self.assertIn("bounds its start loop with delayed restarts", prose)

    def test_0_1_10_records_its_package_version(self):
        self.assertIn("## [0.1.10] - 2026-10-19", TEXT)
        section = TEXT.split("## [0.1.10] - 2026-10-19", 1)[1].split("## [0.1.9]", 1)[0]
        prose = " ".join(section.split())
        self.assertIn("The package version is 0.1.10.", prose)
        self.assertIn("This change does not create a tag.", prose)
        self.assertNotIn("enforces the per-job disk budget during the run", prose)

    def test_0_1_10_records_the_regenerated_seccomp_profile(self):
        section = TEXT.split("## [0.1.10] - 2026-10-19", 1)[1].split("## [0.1.9]", 1)[0]
        prose = " ".join(section.split())
        self.assertIn("is regenerated from the current Docker default profile.", prose)
        self.assertIn("The prior profile had no `clone3` entry", prose)
        self.assertIn("returns ENOSYS (38) without it", prose)
        self.assertIn("adds no `pivot_root` allowance.", prose)
        self.assertIn("`worker --job-privileges` is off by default.", prose)
        self.assertIn("restricted to the values `setarch` needs: 0, 262144, and 0xffffffff", prose)
        self.assertIn("with no unconditional entry.", prose)
        self.assertIn("The capability version stays 12.", prose)

    def test_0_1_9_records_its_package_version(self):
        self.assertIn("## [0.1.9] - 2026-10-19", TEXT)
        section = TEXT.split("## [0.1.9] - 2026-10-19", 1)[1].split("## [0.1.8]", 1)[0]
        prose = " ".join(section.split())
        self.assertIn("The package version is 0.1.9.", prose)
        self.assertIn("This change does not create a tag.", prose)
        self.assertNotIn("is regenerated from the current Docker default profile.", prose)

    def test_0_1_9_records_job_privileges(self):
        section = TEXT.split("## [0.1.9] - 2026-10-19", 1)[1].split("## [0.1.8]", 1)[0]
        prose = " ".join(section.split())
        self.assertIn("`worker --job-privileges` is off by default.", prose)
        self.assertIn("adds only `CAP_SYS_CHROOT`", prose)
        self.assertIn("restricted to the values `setarch` needs: 0, 262144, and 0xffffffff.", prose)
        self.assertIn("The flag composes with `--passwordless-sudo`.", prose)
        self.assertIn("`chroot` takes effect for steps running as root", prose)
        self.assertIn("a bench step that runs Callgrind under `setarch`", prose)
        self.assertIn("The capability version stays 12.", prose)

    def test_0_1_8_records_its_package_version(self):
        self.assertIn("## [0.1.8] - 2026-10-19", TEXT)
        section = TEXT.split("## [0.1.8] - 2026-10-19", 1)[1].split("## [0.1.7]", 1)[0]
        prose = " ".join(section.split())
        self.assertIn("The package version is 0.1.8.", prose)
        self.assertIn("This change does not create a tag.", prose)
        self.assertNotIn("`worker --job-privileges` is off by default.", prose)

    def test_0_1_8_records_the_oidc_token_endpoint(self):
        section = TEXT.split("## [0.1.8] - 2026-10-19", 1)[1].split("## [0.1.7]", 1)[0]
        prose = " ".join(section.split())
        self.assertIn("A per-job token endpoint runs only when the worker is started", prose)
        self.assertIn("with `worker --oidc-issuer`. That flag is off by default.", prose)
        self.assertIn("must grant `id-token: write`, and the run must be trusted.", prose)
        self.assertIn("`ACTIONS_ID_TOKEN_REQUEST_URL`", prose)
        self.assertIn("The capability version stays 12.", prose)

    def test_0_1_8_records_oidc_keys_claims_and_the_hook(self):
        section = TEXT.split("## [0.1.8] - 2026-10-19", 1)[1].split("## [0.1.7]", 1)[0]
        prose = " ".join(section.split())
        self.assertIn("A pre-commit hook rejects an unformatted staged Python file", prose)
        self.assertIn("`oidc publish` writes", prose)
        self.assertIn("`.well-known/jwks.json`", prose)
        self.assertIn("A token lives for five minutes.", prose)
        self.assertIn("refuses a fork, an override run", prose)

    def test_0_1_7_records_its_package_version(self):
        self.assertIn("## [0.1.7] - 2026-10-18", TEXT)
        section = TEXT.split("## [0.1.7] - 2026-10-18", 1)[1].split("## [0.1.6]", 1)[0]
        prose = " ".join(section.split())
        self.assertIn("The package version is 0.1.7.", prose)
        self.assertIn("This change does not create a tag.", prose)
        self.assertNotIn("A per-job token endpoint runs only when", prose)

    def test_0_1_7_records_passwordless_sudo_file_capabilities(self):
        section = TEXT.split("## [0.1.7] - 2026-10-18", 1)[1].split("## [0.1.6]", 1)[0]
        prose = " ".join(section.split())
        self.assertIn("Passwordless sudo still drops every capability.", prose)
        self.assertIn("`CHOWN`, `DAC_OVERRIDE`", prose)
        self.assertIn("stat a mode-0700 workspace", prose)
        self.assertIn("adds no capabilities.", prose)

    def test_0_1_6_records_its_package_version(self):
        self.assertIn("## [0.1.6] - 2026-10-17", TEXT)
        section = TEXT.split("## [0.1.6] - 2026-10-17", 1)[1].split("## [0.1.5]", 1)[0]
        prose = " ".join(section.split())
        self.assertIn("The package version is 0.1.6.", prose)
        self.assertIn("This change does not create a tag.", prose)
        self.assertNotIn("Passwordless sudo still drops every capability.", prose)

    def test_0_1_6_records_step_continue_on_error(self):
        section = TEXT.split("## [0.1.6] - 2026-10-17", 1)[1].split("## [0.1.5]", 1)[0]
        prose = " ".join(section.split())
        self.assertIn("A step `continue-on-error` may be true, false, or an expression", prose)
        self.assertIn("the outcome is failure, the conclusion is success", prose)
        self.assertIn("A later step sees `success()` as true.", prose)
        self.assertIn("False, or an omitted key, still fails the job.", prose)
        self.assertIn("Job `continue-on-error`", prose)
        self.assertIn("The capability version stays 12.", prose)

    def test_0_1_6_records_a_pending_status_after_the_run_finishes(self):
        section = TEXT.split("## [0.1.6] - 2026-10-17", 1)[1].split("## [0.1.5]", 1)[0]
        prose = " ".join(section.split())
        self.assertIn(
            "A poll can record the pending status it already posted when the run has since finished.",
            prose,
        )
        self.assertIn("That record is not a terminal state.", prose)
        self.assertIn("A later pass still posts the finished state.", prose)
        self.assertIn("A terminal state that does not match the run is still refused.", prose)

    def test_0_1_4_records_the_poll_skip_and_the_mit_license(self):
        self.assertIn("## [0.1.4] - 2026-10-14", TEXT)
        section = TEXT.split("## [0.1.4] - 2026-10-14", 1)[1].split("## [0.1.3]", 1)[0]
        prose = " ".join(section.split())
        self.assertIn("That run is skipped and its submission stays.", prose)
        self.assertIn("The project license is the MIT License.", prose)
        self.assertIn("This change does not create a tag.", prose)

    def test_0_1_3_records_the_network_default_and_the_upgrade(self):
        self.assertIn("## [0.1.3] - 2026-10-11", TEXT)
        self.assertNotIn("## [0.1.2]", TEXT)
        section = TEXT.split("## [0.1.3] - 2026-10-11", 1)[1].split("## [0.1.1]", 1)[0]
        prose = " ".join(section.split())
        self.assertIn("Job containers use Docker network `none` by default.", prose)
        self.assertIn("`worker --network bridge` opts in to egress.", prose)
        self.assertIn("Service containers start only with that opt-in.", prose)
        self.assertIn(
            "A host that upgrades onto a tree where the job network defaults to `none`",
            prose,
        )
        self.assertIn(
            "adds `--network bridge` on that host's unit when jobs need egress.",
            prose,
        )
        self.assertIn("Cargo and pip need it.", prose)
        self.assertIn("The repository unit omits the flag.", prose)
        self.assertIn("This change does not create a tag.", prose)

    def test_0_1_1_records_the_tag_fetch_fix(self):
        self.assertIn("## [0.1.1] - 2026-10-10", TEXT)
        self.assertIn("fetches the annotated tag", TEXT)
        self.assertIn("names the wheel and tarball hashes", PROSE)
        self.assertNotIn("## [0.1.0]", TEXT)


if __name__ == "__main__":
    unittest.main()
