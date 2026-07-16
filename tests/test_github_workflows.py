"""Smoke tests on .github/workflows/*.yaml (Issue #29 CI wiring): confirms
they're valid YAML and have the shape CLAUDE.md/ADR discipline describes --
build-images.yaml builds/pushes images only (never touches deploy/k8s,
never applies :latest -- rollout is ArgoCD's job), config-validate.yaml
runs config validation + the unittest suite on config/** PRs.

PyYAML's SafeLoader parses a bare `on:` key as the YAML 1.1 boolean `True`
(a well-known GitHub Actions YAML gotcha), so this file's assertions look
that key up as `True` rather than the string "on".
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"


def _load(name: str) -> dict:
    path = WORKFLOWS_DIR / name
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class BuildImagesWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = _load("build-images.yaml")

    def test_parses_as_yaml_with_jobs(self) -> None:
        self.assertIn("jobs", self.doc)
        self.assertTrue(self.doc["jobs"])

    def test_triggers_on_push_to_main_and_manual_dispatch(self) -> None:
        triggers = self.doc[True]  # `on:` parses as boolean True key
        self.assertIn("push", triggers)
        self.assertEqual(triggers["push"]["branches"], ["main"])
        self.assertIn("workflow_dispatch", triggers)

    def _non_comment_lines(self) -> str:
        raw = (WORKFLOWS_DIR / "build-images.yaml").read_text(encoding="utf-8")
        return "\n".join(line for line in raw.splitlines() if not line.strip().startswith("#"))

    def test_never_touches_deploy_k8s_paths(self) -> None:
        """CD stays out of GitHub Actions (ADR discipline): this workflow's
        *executable* content -- not its explanatory comments -- must never
        reference deploy/k8s, kubectl apply, or argocd."""
        code = self._non_comment_lines()
        self.assertNotIn("deploy/k8s", code)
        self.assertNotIn("kubectl apply", code)
        self.assertNotIn("argocd", code.lower())

    def test_never_pushes_latest_tag(self) -> None:
        code = self._non_comment_lines()
        self.assertNotIn(":latest", code)

    def test_image_tags_reference_the_computed_short_sha_step_output(self) -> None:
        """Issue #29 CONCERNS fix (P1-1): tags must come from the
        `Compute short SHA tag` step's output, not `sha-${{ github.sha }}`
        inlined directly -- the latter pushes a 40-char tag while
        deploy/k8s pins a 12-char short SHA, which is a permanent
        ImagePullBackOff (ghcr.io never has an image at the tag the
        manifest asks for)."""
        for job in self.doc["jobs"].values():
            step_ids = {step.get("id") for step in job.get("steps", [])}
            self.assertIn("tag", step_ids, f"job {job} missing the short-SHA 'tag' step")
            for step in job.get("steps", []):
                tags = (step.get("with") or {}).get("tags")
                if tags:
                    self.assertIn("steps.tag.outputs.value", tags)
                    self.assertNotIn("github.sha", tags)

    def test_short_sha_computed_from_github_sha_env_using_short_sha_length(self) -> None:
        for job in self.doc["jobs"].values():
            tag_steps = [step for step in job.get("steps", []) if step.get("id") == "tag"]
            self.assertEqual(len(tag_steps), 1)
            run = tag_steps[0].get("run", "")
            self.assertIn("GITHUB_SHA:0:$SHORT_SHA_LENGTH", run)
            self.assertIn("sha-", run)

    def test_python_and_go_proxy_image_matrices_cover_expected_images(self) -> None:
        jobs = self.doc["jobs"]
        python_images = {entry["name"] for entry in jobs["build-python-images"]["strategy"]["matrix"]["image"]}
        self.assertEqual(python_images, {"7mimi-agent-scheduler", "7mimi-agent-agent-runner"})

        go_services = set(jobs["build-go-proxy-images"]["strategy"]["matrix"]["service"])
        self.assertEqual(go_services, {"claude-proxy", "auth-proxy", "egress-proxy"})

    def test_packages_write_permission_present_for_ghcr_push(self) -> None:
        self.assertEqual(self.doc.get("permissions", {}).get("packages"), "write")


class ImageTagFormatConsistencyTest(unittest.TestCase):
    """Issue #29 CONCERNS fix (P1-1): the tag *shape* CI pushes to ghcr.io
    and the tag *shape* deploy/k8s manifests reference must match, or every
    rollout silently ImagePullBackOffs. Checked at the shape level (both are
    'sha-' + N lowercase hex chars, same N) rather than computing an actual
    git SHA, since this test doesn't run inside the CI job that knows the
    real commit -- it's the manifests' *placeholder* tag that must already
    conform to CI's output shape so a real push slots in correctly."""

    def setUp(self) -> None:
        self.short_sha_length = int(_load("build-images.yaml")["env"]["SHORT_SHA_LENGTH"])
        self.tag_pattern = re.compile(rf"^sha-[0-9a-f]{{{self.short_sha_length}}}$")

    def _kustomization(self) -> dict:
        return _load_path(REPO_ROOT / "deploy" / "k8s" / "kustomization.yaml")

    def _scheduler_docs(self) -> list[dict]:
        path = REPO_ROOT / "deploy" / "k8s" / "scheduler.yaml"
        with path.open("r", encoding="utf-8") as fh:
            return [doc for doc in yaml.safe_load_all(fh) if doc]

    def test_kustomization_image_tags_match_ci_short_sha_shape(self) -> None:
        kustomization = self._kustomization()
        images = kustomization.get("images", [])
        self.assertTrue(images, "expected deploy/k8s/kustomization.yaml to pin image tags")
        for image in images:
            self.assertRegex(
                image["newTag"],
                self.tag_pattern,
                f"{image['name']} newTag {image['newTag']!r} must match CI's sha-<{self.short_sha_length} hex> shape",
            )

    def test_scheduler_runner_image_env_tag_matches_ci_short_sha_shape(self) -> None:
        """agent-runner isn't in kustomization.yaml's `images:` (Jobs are
        created dynamically, not declared statically) -- its tag lives on
        the scheduler Deployment's RUNNER_IMAGE env var instead."""
        scheduler = next(d for d in self._scheduler_docs() if d.get("kind") == "Deployment")
        env = {e["name"]: e.get("value") for e in scheduler["spec"]["template"]["spec"]["containers"][0]["env"]}
        runner_image = env.get("RUNNER_IMAGE")
        self.assertIsNotNone(runner_image, "scheduler.yaml must set RUNNER_IMAGE")
        tag = runner_image.rsplit(":", 1)[1]
        self.assertRegex(
            tag,
            self.tag_pattern,
            f"RUNNER_IMAGE tag {tag!r} must match CI's sha-<{self.short_sha_length} hex> shape",
        )


def _load_path(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class ConfigValidateWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = _load("config-validate.yaml")

    def test_parses_as_yaml_with_jobs(self) -> None:
        self.assertIn("jobs", self.doc)
        self.assertTrue(self.doc["jobs"])

    def test_triggers_on_pull_request_touching_config(self) -> None:
        triggers = self.doc[True]
        self.assertIn("pull_request", triggers)
        self.assertIn("config/**", triggers["pull_request"]["paths"])

    def test_runs_config_validate_and_unittest_discover(self) -> None:
        raw = (WORKFLOWS_DIR / "config-validate.yaml").read_text(encoding="utf-8")
        self.assertIn("shichimimi_agent config validate", raw)
        self.assertIn("unittest discover -s tests", raw)

    def test_uses_pythonpath_src_env(self) -> None:
        raw = (WORKFLOWS_DIR / "config-validate.yaml").read_text(encoding="utf-8")
        self.assertIn("PYTHONPATH: src", raw)

    def test_read_only_contents_permission(self) -> None:
        self.assertEqual(self.doc.get("permissions", {}).get("contents"), "read")


class TestWorkflowTest(unittest.TestCase):
    """Issue #29 CONCERNS fix (P3-6): unittest must run on every PR and
    every push to main, not just PRs that touch config/** (config-validate
    covers that narrower path already)."""

    def setUp(self) -> None:
        self.doc = _load("test.yaml")

    def test_parses_as_yaml_with_jobs(self) -> None:
        self.assertIn("jobs", self.doc)
        self.assertTrue(self.doc["jobs"])

    def test_triggers_on_every_pull_request_and_main_push(self) -> None:
        triggers = self.doc[True]
        self.assertIn("pull_request", triggers)
        # Deliberately no `paths:` filter -- unlike config-validate.yaml,
        # this must run for every PR regardless of which paths changed.
        self.assertNotIn("paths", triggers["pull_request"])
        self.assertIn("push", triggers)
        self.assertEqual(triggers["push"]["branches"], ["main"])

    def test_runs_unittest_discover(self) -> None:
        raw = (WORKFLOWS_DIR / "test.yaml").read_text(encoding="utf-8")
        self.assertIn("unittest discover -s tests", raw)

    def test_uses_pythonpath_src_env(self) -> None:
        raw = (WORKFLOWS_DIR / "test.yaml").read_text(encoding="utf-8")
        self.assertIn("PYTHONPATH: src", raw)

    def test_read_only_contents_permission(self) -> None:
        self.assertEqual(self.doc.get("permissions", {}).get("contents"), "read")


if __name__ == "__main__":
    unittest.main()
