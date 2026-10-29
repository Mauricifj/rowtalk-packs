"""Static checks on the workflows (stdlib only — there is no YAML parser in
the standard library, so these are line-level checks of the properties that
matter for I3 and supply-chain hygiene)."""

import glob
import os
import re
import unittest

from tests.helpers import REPO

WORKFLOWS = sorted(glob.glob(os.path.join(REPO, ".github", "workflows", "*.yml")))
USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)(.*)$")


def jobs(text):
    """{job name: job body text} from the top-level `jobs:` mapping."""
    body = text.split("\njobs:\n", 1)[1]
    out = {}
    current = None
    for line in body.splitlines():
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if match:
            current = match.group(1)
            out[current] = ""
        elif current:
            out[current] += line + "\n"
    return out


def step_script(job_body, step_name):
    """The `run: |` block of the step whose name starts with `step_name`,
    de-indented (the shell script GitHub would run)."""
    step = next(st for st in job_body.split("\n      - ") if st.startswith("name: " + step_name))
    lines = step.split("\n        run: |\n", 1)[1].splitlines()
    script = []
    for line in lines:
        if line.strip() and not line.startswith("          "):
            break
        script.append(line[10:])
    return "\n".join(script) + "\n"


class WorkflowTest(unittest.TestCase):
    def test_the_workflows_exist(self):
        names = {os.path.basename(p) for p in WORKFLOWS}
        self.assertEqual(names, {"ci.yml", "probe.yml", "build-wiki.yml", "build-dict.yml",
                                 "keepalive.yml"})

    def test_actions_are_pinned_by_full_sha_with_version_comment(self):
        for path in WORKFLOWS:
            with open(path, "r", encoding="utf-8") as handle:
                for number, line in enumerate(handle, 1):
                    match = USES.match(line)
                    if not match:
                        continue
                    ref, rest = match.groups()
                    self.assertRegex(ref, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$", "%s:%d" % (path, number))
                    self.assertRegex(rest, r"#\s*v\d+\.\d+\.\d+", "%s:%d" % (path, number))

    def test_every_job_has_a_timeout(self):
        for path in WORKFLOWS:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            for name, body in jobs(text).items():
                self.assertRegex(body, r"\n    timeout-minutes: \d+\n", "%s job %s" % (path, name))

    def test_default_permissions_are_read_only(self):
        for path in WORKFLOWS:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("\npermissions:\n  contents: read\n", text, path)

    def test_read_only_workflows_never_ask_for_write(self):
        for name in ("ci.yml", "probe.yml"):
            with open(os.path.join(REPO, ".github", "workflows", name), "r", encoding="utf-8") as h:
                self.assertNotRegex(h.read(), r":\s*write\b")

    def test_publishing_workflows_share_one_concurrency_group(self):
        for name in ("build-wiki.yml", "build-dict.yml"):
            with open(os.path.join(REPO, ".github", "workflows", name), "r", encoding="utf-8") as h:
                text = h.read()
            self.assertIn("group: rowtalk-packs-publish", text)
            self.assertIn("cancel-in-progress: false", text)

    def test_wiki_build_parallelism_bounds_dump_connections(self):
        import json
        with open(os.path.join(REPO, ".github", "workflows", "build-wiki.yml"), "r",
                  encoding="utf-8") as handle:
            text = handle.read()
        with open(os.path.join(REPO, "config", "packs.json"), "r", encoding="utf-8") as handle:
            dump = json.load(handle)["dump"]
        build = jobs(text)["build"]
        parallel = int(re.search(r"max-parallel: (\d+)", build).group(1))
        # I3: concurrent build jobs x downloaders per job <= the per-run cap.
        self.assertLessEqual(parallel * dump["connections_per_job"],
                             dump["max_connections_per_run"])
        # The probe's go/no-go compares against config; it must BE the timeout.
        with open(os.path.join(REPO, "config", "packs.json"), "r", encoding="utf-8") as handle:
            timeout = json.load(handle)["wiki"]["build_timeout_minutes"]
        self.assertIn("\n    timeout-minutes: %d\n" % timeout, build)
        # OPS-3: one language failing must not cancel the other.
        self.assertIn("fail-fast: false", build)
        self.assertIn("always() && !cancelled()", jobs(text)["publish"])
        self.assertRegex(text, r'cron: "\d{1,2} \d{1,2} ')  # off-the-hour minute
        self.assertNotRegex(text, r'cron: "0 ')

    def test_publishing_jobs_refuse_a_non_main_ref(self):
        # OPS-2: the job that can delete or skip releases runs only on main.
        guard = "if: ${{ !inputs.dry_run && github.ref != 'refs/heads/main' }}"
        for name, job_names in (("build-wiki.yml", ("resolve", "rollback")),
                                ("build-dict.yml", ("dict",))):
            with open(os.path.join(REPO, ".github", "workflows", name), "r", encoding="utf-8") as h:
                body = jobs(h.read())
            for job in job_names:
                first_step = body[job].split("    steps:\n", 1)[1].split("\n      - ", 2)[0]
                self.assertIn(guard, first_step, "%s %s" % (name, job))

    def test_keepalive_survives_dropped_schedules_and_is_never_cancelled(self):
        # OPS2-04: scheduled runs can be dropped, so the margin must survive
        # several misses. Weekly cron: gap between executed runs = 7 x
        # (missed + 1); worst idle at the next executed run = DAYS + gap.
        with open(os.path.join(REPO, ".github", "workflows", "keepalive.yml"), "r",
                  encoding="utf-8") as handle:
            text = handle.read()
        cron = re.search(r'cron: "(\S+) (\S+) (\S+) (\S+) (\S+)"', text).groups()
        self.assertNotEqual(cron[0], "0")  # off the hour
        self.assertEqual(cron[2:4], ("*", "*"))
        self.assertRegex(cron[4], r"^[0-6]$")  # one weekday: runs are exactly 7 days apart
        days = int(re.search(r'KEEPALIVE_DAYS: "(\d+)"', text).group(1))
        tolerated_misses = 4
        schedule_delay_margin = 2
        self.assertLess(days + 7 * (tolerated_misses + 1) + schedule_delay_margin, 60)
        self.assertIn('-lt "$KEEPALIVE_DAYS"', text)
        # Its own group: a pending publish run can never cancel it.
        self.assertIn("group: rowtalk-packs-keepalive", text)
        self.assertNotIn("rowtalk-packs-publish", text)
        self.assertIn("contents: write", jobs(text)["heartbeat"])
        # OPS3-04: the bot-push premise is unverified, so the second belt
        # (the documented enable API, every run, for every scheduled
        # workflow) must exist, run even after a failed heartbeat push, and
        # the premise must be stated as such.
        heartbeat = jobs(text)["heartbeat"]
        self.assertIn("actions: write", heartbeat)
        belt = heartbeat.split("name: Re-enable the scheduled workflows", 1)[1]
        self.assertIn("if: ${{ !cancelled() }}", belt)
        self.assertIn('gh api -X PUT "repos/$REPO/actions/workflows/$workflow/enable"', belt)
        scheduled = []
        for path in WORKFLOWS:
            with open(path, "r", encoding="utf-8") as handle:
                if re.search(r"^\s+schedule:", handle.read(), re.M):
                    scheduled.append(os.path.basename(path))
        self.assertEqual(len(scheduled), 3)
        for name in scheduled:
            self.assertIn(name, belt.split("for workflow in", 1)[1].split(";", 1)[0])
        self.assertIn("UNVERIFIED PREMISE", text)
        # And the publishing workflows no longer carry their own copy.
        with open(os.path.join(REPO, ".github", "workflows", "build-wiki.yml"), "r",
                  encoding="utf-8") as handle:
            self.assertNotIn("keepalive", jobs(handle.read()))

    def test_keepalive_never_re_enables_a_manually_disabled_workflow(self):
        # OPS4-01: PUT .../enable turns ANY state active, so an unconditional
        # belt undid an operator's pause within a week. Run the step's REAL
        # script against a stub `gh` and see which workflows it enables.
        import shutil
        import subprocess
        import tempfile
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash not available")
        with open(os.path.join(REPO, ".github", "workflows", "keepalive.yml"), "r",
                  encoding="utf-8") as handle:
            script = step_script(jobs(handle.read())["heartbeat"],
                                 "Re-enable the scheduled workflows")
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        calls = os.path.join(work, "calls.txt")
        with open(os.path.join(work, "gh"), "w", encoding="utf-8") as handle:
            handle.write("""#!/bin/sh
# Stub: GET .../workflows/<file> --jq .state prints the scripted state; a PUT
# .../enable is recorded. STATE_<file stem> scripts each workflow's state;
# 'fail' makes the GET fail.
echo "$*" >> "$CALLS"
for arg in "$@"; do case "$arg" in repos/*) path="$arg";; esac; done
wf=$(basename "${path%/enable}" .yml | tr '-' '_')
if [ "$1" = "-X" ]; then exit 0; fi
eval "state=\\$STATE_$wf"
if [ "$state" = "fail" ]; then exit 1; fi
echo "$state"
""")
        os.chmod(os.path.join(work, "gh"), 0o755)

        def run(states):
            if os.path.exists(calls):
                os.remove(calls)
            env = dict(os.environ, PATH=work + os.pathsep + os.environ.get("PATH", ""),
                       CALLS=calls, REPO="o/r", GH_TOKEN="x")
            env.update({"STATE_" + k.replace("-", "_"): v for k, v in states.items()})
            done = subprocess.run([bash, "-e", "-c", script], env=env, capture_output=True,
                                  text=True, timeout=30)
            with open(calls, "r", encoding="utf-8") as handle:
                put = sorted(line.split("workflows/")[1].split("/enable")[0]
                             for line in handle if line.startswith("api -X PUT"))
            return done.returncode, put, done.stdout

        code, put, out = run({"build-wiki": "disabled_manually", "build-dict": "active",
                              "keepalive": "disabled_inactivity"})
        self.assertEqual(code, 0, out)
        self.assertEqual(put, ["build-dict.yml", "keepalive.yml"])
        self.assertRegex(out, r"::warning::build-wiki\.yml was disabled by hand")
        # Unknown states are left alone; an unreadable state is red but the
        # other workflows are still handled.
        code, put, out = run({"build-wiki": "fail", "build-dict": "deleted",
                              "keepalive": "active"})
        self.assertEqual(code, 1, out)
        self.assertEqual(put, ["keepalive.yml"])

    def test_a_dry_run_rollback_commits_nothing(self):
        # OPS2-01: dry_run skips the non-main guard, so the commit step must
        # itself be dry_run-gated, and the CLI must be told it is a dry run.
        # DC4-03: build-dict has the same rollback job for dict families.
        for name in ("build-wiki.yml", "build-dict.yml"):
            with open(os.path.join(REPO, ".github", "workflows", name), "r",
                      encoding="utf-8") as handle:
                all_jobs = jobs(handle.read())
            body = all_jobs["rollback"]
            self.assertIn("if: ${{ inputs.rollback_to }}", body, name)
            # A rollback builds nothing: every other ROOT job (no `needs:`;
            # dependents are skipped with it) is off.
            for job, job_body in all_jobs.items():
                if job != "rollback" and "\n    needs:" not in "\n" + job_body:
                    self.assertIn("if: ${{ !inputs.rollback_to }}", job_body, (name, job))
            steps = body.split("\n      - ")
            repoint = next(st for st in steps if st.startswith("name: Re-point manifest"))
            self.assertIn("DRY_RUN: ${{ inputs.dry_run && '--dry-run' || '' }}", repoint, name)
            self.assertIn("$DRY_RUN", repoint, name)
            commit = next(st for st in steps if st.startswith("name: Commit manifest"))
            self.assertIn("if: ${{ !inputs.dry_run }}", commit, name)
            self.assertIn("git push", commit, name)

    def test_dict_rollback_derives_the_family_from_the_tag(self):
        # DC4-03: run the Re-point step's REAL script with a stub `python`
        # and check which family it passes (and that a non-dict tag stops).
        import shutil
        import subprocess
        import tempfile
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash not available")
        with open(os.path.join(REPO, ".github", "workflows", "build-dict.yml"), "r",
                  encoding="utf-8") as handle:
            script = step_script(jobs(handle.read())["rollback"], "Re-point manifest")
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        with open(os.path.join(work, "python"), "w", encoding="utf-8") as handle:
            handle.write('#!/bin/sh\necho "ARGS $*"\n')
        os.chmod(os.path.join(work, "python"), 0o755)

        def run(tag):
            env = dict(os.environ, PATH=work + os.pathsep + os.environ.get("PATH", ""),
                       TAG=tag, DRY_RUN="--dry-run")
            return subprocess.run([bash, "-e", "-c", script], env=env, capture_output=True,
                                  text=True, timeout=30)
        for tag, family in (("dict-en-2024-edition", "dict-en"), ("dict-pt-v1.1.0", "dict-pt"),
                            ("dict-en-2025-edition-r2", "dict-en")):
            done = run(tag)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("ARGS -m rowtalk_packs rollback --tag %s --families %s --dry-run"
                          % (tag, family), done.stdout)
        for tag in ("wiki-2026.10.18", "dict-", "x; rm -rf /"):
            done = run(tag)
            self.assertNotEqual(done.returncode, 0, tag)
            self.assertNotIn("ARGS", done.stdout, tag)

    def test_dict_commit_runs_after_a_partial_failure(self):
        # OPS2-05: a language that published must reach main even when the
        # step failed for another language.
        with open(os.path.join(REPO, ".github", "workflows", "build-dict.yml"), "r",
                  encoding="utf-8") as handle:
            body = jobs(handle.read())["dict"]
        steps = body.split("\n      - ")
        for name in ("name: Commit manifest", "name: Prune superseded"):
            step = next(st for st in steps if st.startswith(name))
            self.assertIn("if: ${{ !cancelled() && steps.dict.outputs.changed == 'true' }}",
                          step, name)

    def test_ci_log_shows_the_verdict(self):
        # FR5-02: the visible CI log must carry unittest's verdict and, on a
        # red run, every failure — not the tail of buffered probe output.
        import subprocess
        import tempfile
        with open(os.path.join(REPO, ".github", "workflows", "ci.yml"), "r",
                  encoding="utf-8") as handle:
            text = handle.read()
        self.assertRegex(text, r'(?m)^env:\n(?:  #.*\n)*  PYTHONUNBUFFERED: "1"$')
        script = step_script(jobs(text)["test"], "Unit tests")
        with tempfile.TemporaryDirectory() as work:
            fake = os.path.join(work, "python")
            with open(fake, "w", encoding="utf-8") as handle:
                handle.write('#!/bin/sh\necho "FAIL: test_early (x)"\n'
                             'i=0; while [ $i -lt 200 ]; do echo filler; i=$((i+1)); done\n'
                             'echo "FAILED (failures=1)"; exit 1\n')
            os.chmod(fake, 0o755)
            env = dict(os.environ, PATH=work + os.pathsep + os.environ.get("PATH", ""))
            done = subprocess.run(["bash", "-e", "-c", script], cwd=work, env=env,
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 1)
        self.assertIn("FAIL: test_early (x)", done.stdout)
        self.assertIn("FAILED (failures=1)", done.stdout)

    def test_python_pinned_to_312(self):
        for path in WORKFLOWS:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            for version in re.findall(r'python-version:\s*"([^"]+)"', text):
                self.assertEqual(version, "3.12", path)

    def test_runner_image_is_pinned(self):
        # `ubuntu-latest` began moving to Ubuntu 26 on 2026-10-19 (the
        # first probe's annotation); a scheduled build must not change
        # operating system — and setup-python's 3.12 build — unannounced.
        for path in WORKFLOWS:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            labels = re.findall(r"^\s*runs-on:\s*(\S+)\s*$", text, re.M)
            self.assertTrue(labels, path)
            for label in labels:
                self.assertEqual(label, "ubuntu-24.04", path)


if __name__ == "__main__":
    unittest.main()
