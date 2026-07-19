"""Structural checks on the deploy/k8s Kustomize manifests (Issue #29 / k3s
+ ArgoCD deploy).

Mirrors the style of test_compose_config.py, but rendered via `kubectl
kustomize` since Kustomize resolves configMapGenerator hashes, image tag
overrides, and cross-file references (namespace, RoleBinding subjects,
etc.) that a per-file PyYAML parse would miss. Skips (not fails) when
`kubectl` isn't on PATH, matching this repo's convention of not making CI
tooling availability a hard requirement for local test runs.

Verification points (see Issue #29 / the KubernetesRunnerBackend docstring
in kubernetes_runner.py for the design these encode):
- no Secret object's material is committed -- only secretKeyRef/volume
  references to a Secret this repo never creates.
- no `:latest` image tag anywhere (images are pinned to git-SHA tags).
- the runner NetworkPolicy's egress is exactly the 3 proxies + kube-dns.
- the scheduler's Role has no `watch`/`delete` verbs on Jobs (Jobs clean
  themselves up via ttlSecondsAfterFinished; the backend polls, not watches).
- the Namespace disables istio sidecar injection.
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
K8S_DIR = REPO_ROOT / "deploy" / "k8s"

# Matches kubernetes_runner.py's KubernetesRunnerOptions default runner_label
# and the job_labels it stamps on runner Jobs -- kept here as an explicit
# string (not an import) so this test also catches accidental drift between
# the manifest's NetworkPolicy podSelector and the code's default without
# needing to import runner internals into a manifests test.
RUNNER_POD_LABEL = "7mimi-agent-runner"


def _kustomize_build() -> list[dict]:
    result = subprocess.run(
        ["kubectl", "kustomize", "--load-restrictor", "LoadRestrictionsNone", str(K8S_DIR)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"kubectl kustomize failed: {result.stderr}")
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


@unittest.skipUnless(shutil.which("kubectl"), "kubectl not available")
class KustomizeRenderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.docs = _kustomize_build()
        self.by_kind: dict[str, list[dict]] = {}
        for doc in self.docs:
            self.by_kind.setdefault(doc.get("kind"), []).append(doc)

    def test_renders_without_error_and_has_expected_kinds(self) -> None:
        kinds = set(self.by_kind.keys())
        self.assertIn("Namespace", kinds)
        self.assertIn("Deployment", kinds)
        self.assertIn("NetworkPolicy", kinds)
        self.assertIn("PersistentVolumeClaim", kinds)
        self.assertIn("ConfigMap", kinds)

    def test_no_secret_object_rendered(self) -> None:
        """Secret material (ANTHROPIC_API_KEY, GITHUB_APP_ID, ...) must
        never be a manifest resource in this repo -- only secretKeyRef /
        volume references to a Secret created out-of-band on the cluster."""
        self.assertNotIn("Secret", self.by_kind)

    def test_env_secrets_use_secret_key_ref_not_literal_values(self) -> None:
        secret_keys = {
            "ANTHROPIC_API_KEY",
            "AUTH_PROXY_SESSION_TOKEN",
            "X_BEARER_TOKEN",
            "GITHUB_APP_ID",
            "GITHUB_APP_INSTALLATION_ID",
            "SLACK_BOT_TOKEN",
            "SLACK_CHANNEL_ID",
            "SLACK_SYSLOG_CHANNEL_ID",
            "CLAUDE_PROXY_SESSION_TOKEN",
        }
        for deployment in self.by_kind.get("Deployment", []):
            containers = deployment["spec"]["template"]["spec"]["containers"]
            for container in containers:
                for env in container.get("env") or []:
                    if env["name"] in secret_keys:
                        self.assertIn(
                            "valueFrom",
                            env,
                            f"{deployment['metadata']['name']}.{container['name']}.env.{env['name']} "
                            "must use valueFrom.secretKeyRef, not a literal value",
                        )
                        self.assertIn("secretKeyRef", env["valueFrom"])

    def test_claude_proxy_dev_token_wired_to_same_secret_key_as_scheduler(self) -> None:
        """claude-proxy validates the scheduler's Bearer against its
        CLAUDE_PROXY_DEV_TOKEN env (services/claude-proxy config). Both
        sides must read the SAME out-of-band secret key, or every
        scheduler request 401s against the compiled-in dev default."""
        envs: dict[str, dict] = {}
        for deployment in self.by_kind.get("Deployment", []):
            name = deployment["metadata"]["name"]
            for container in deployment["spec"]["template"]["spec"]["containers"]:
                for env in container.get("env") or []:
                    envs[f"{name}/{env['name']}"] = env
        proxy_side = envs.get("claude-proxy/CLAUDE_PROXY_DEV_TOKEN")
        scheduler_side = envs.get("scheduler/CLAUDE_PROXY_SESSION_TOKEN")
        self.assertIsNotNone(proxy_side, "claude-proxy must wire CLAUDE_PROXY_DEV_TOKEN")
        self.assertIsNotNone(scheduler_side, "scheduler must wire CLAUDE_PROXY_SESSION_TOKEN")
        self.assertEqual(
            proxy_side["valueFrom"]["secretKeyRef"],
            scheduler_side["valueFrom"]["secretKeyRef"],
            "claude-proxy CLAUDE_PROXY_DEV_TOKEN and scheduler CLAUDE_PROXY_SESSION_TOKEN "
            "must reference the same Secret name/key",
        )

    def test_no_latest_image_tag(self) -> None:
        for deployment in self.by_kind.get("Deployment", []):
            for container in deployment["spec"]["template"]["spec"]["containers"]:
                image = container["image"]
                self.assertNotEqual(image.split(":")[-1], "latest", image)
                self.assertIn(":", image, f"{image} has no tag at all")

    def test_runner_job_image_pin_in_scheduler_env_is_not_latest(self) -> None:
        """agent-runner's tag lives on the scheduler Deployment's
        RUNNER_IMAGE env var (Jobs are created dynamically, not declared
        here) -- check it directly since it isn't a container `image:`
        field kustomize's `images:` transformer would rewrite."""
        scheduler = next(d for d in self.by_kind["Deployment"] if d["metadata"]["name"] == "scheduler")
        env = {e["name"]: e.get("value") for e in scheduler["spec"]["template"]["spec"]["containers"][0]["env"]}
        runner_image = env.get("RUNNER_IMAGE")
        self.assertIsNotNone(runner_image)
        self.assertNotEqual(runner_image.split(":")[-1], "latest", runner_image)

    def test_runner_network_policy_egress_is_exactly_three_proxies_and_kube_dns(self) -> None:
        policy = next(p for p in self.by_kind["NetworkPolicy"] if p["metadata"]["name"] == "runner-default-deny")
        self.assertEqual(policy["spec"]["ingress"], [])

        egress = policy["spec"]["egress"]
        all_ports = sorted(p["port"] for rule in egress for p in rule.get("ports", []))
        self.assertEqual(all_ports, [53, 53, 18080, 18081, 18082])

        # No rule with an empty `to: []` (would mean unrestricted egress to
        # any destination on those ports) is present for the runner policy
        # -- unlike scheduler-egress's apiserver rule, runner Jobs must not
        # reach kube-apiserver or anything outside proxies + dns.
        for rule in egress:
            self.assertNotEqual(rule.get("to"), [], "runner egress must not contain an unrestricted `to: []` rule")

    def test_runner_network_policy_pod_selector_matches_runner_label(self) -> None:
        policy = next(p for p in self.by_kind["NetworkPolicy"] if p["metadata"]["name"] == "runner-default-deny")
        self.assertEqual(
            policy["spec"]["podSelector"]["matchLabels"].get("app.kubernetes.io/name"),
            RUNNER_POD_LABEL,
        )

    def test_scheduler_role_has_no_watch_or_delete_verbs_on_jobs(self) -> None:
        role = next(r for r in self.by_kind["Role"] if r["metadata"]["name"] == "7mimi-scheduler-runner-jobs")
        for rule in role["rules"]:
            if "batch" in rule.get("apiGroups", []) and "jobs" in rule.get("resources", []):
                self.assertNotIn("watch", rule["verbs"])
                self.assertNotIn("delete", rule["verbs"])

    def test_scheduler_role_pods_access_is_get_only_no_list_no_log(self) -> None:
        """Issue #29 reviewer CONCERNS fix (P2-3): the scheduler only ever
        GETs its own Pod by name (self-lookup for the mounted ConfigMap
        name, see kubernetes_runner.py `_resolve_configmap_name`) -- it
        never lists pods and never reads pod logs (results come from the
        shared PVC's result.json, not Pod logs), so RBAC must not grant
        either."""
        role = next(r for r in self.by_kind["Role"] if r["metadata"]["name"] == "7mimi-scheduler-runner-jobs")
        pods_rules = [r for r in role["rules"] if "pods" in r.get("resources", []) and "" in r.get("apiGroups", [""])]
        self.assertTrue(pods_rules, "expected a core-apiGroup rule granting access to pods")
        for rule in pods_rules:
            self.assertEqual(rule["verbs"], ["get"])

        log_rules = [r for r in role["rules"] if "pods/log" in r.get("resources", [])]
        self.assertEqual(log_rules, [], "pods/log must not be granted -- results come from the shared PVC, not logs")

    def test_networkpolicy_comment_accurately_describes_k3s_default_enforcement(self) -> None:
        """The comment must not claim k3s's default CNI leaves NetworkPolicy
        inert -- k3s's embedded kube-router netpol controller enforces it by
        default (only `--disable-network-policy` turns that off)."""
        raw = (K8S_DIR / "networkpolicy.yaml").read_text(encoding="utf-8")
        self.assertIn("kube-router", raw)
        self.assertIn("--disable-network-policy", raw)
        self.assertNotIn("does NOT enforce NetworkPolicy", raw)
        self.assertNotIn("policies are inert unless", raw)

    def test_scheduler_and_proxy_deployments_have_restricted_security_context(self) -> None:
        """Issue #29 reviewer CONCERNS fix (P2-4): Pod Security
        "restricted"-equivalent securityContext on every static Deployment
        (the runner Job's equivalent is asserted in test_kubernetes_runner.py
        since Jobs aren't declared statically here)."""
        for deployment in self.by_kind["Deployment"]:
            name = deployment["metadata"]["name"]
            pod_spec = deployment["spec"]["template"]["spec"]
            pod_security = pod_spec.get("securityContext")
            self.assertIsNotNone(pod_security, f"{name}: missing pod securityContext")
            self.assertIs(pod_security.get("runAsNonRoot"), True, name)
            self.assertEqual(pod_security.get("seccompProfile"), {"type": "RuntimeDefault"}, name)

            for container in pod_spec["containers"]:
                container_security = container.get("securityContext")
                self.assertIsNotNone(container_security, f"{name}/{container['name']}: missing container securityContext")
                self.assertIs(container_security.get("allowPrivilegeEscalation"), False, name)
                self.assertEqual(container_security.get("capabilities"), {"drop": ["ALL"]}, name)

    def test_scheduler_deployment_pins_explicit_non_root_uid_matching_runner_env(self) -> None:
        """scheduler.yaml's securityContext.runAsUser/runAsGroup must equal
        the RUNNER_UID/RUNNER_GID it hands to KubernetesRunnerBackend --
        both sides read/write the same PVC subPath directories, so a
        mismatch would reintroduce cross-container permission errors."""
        scheduler = next(d for d in self.by_kind["Deployment"] if d["metadata"]["name"] == "scheduler")
        pod_security = scheduler["spec"]["template"]["spec"]["securityContext"]
        self.assertEqual(pod_security["runAsUser"], 10001)
        self.assertEqual(pod_security["runAsGroup"], 10001)
        self.assertEqual(pod_security["fsGroup"], 10001)

        env = {e["name"]: e.get("value") for e in scheduler["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env.get("RUNNER_UID"), str(pod_security["runAsUser"]))
        self.assertEqual(env.get("RUNNER_GID"), str(pod_security["runAsGroup"]))

    def test_namespace_disables_istio_injection(self) -> None:
        namespace = self.by_kind["Namespace"][0]
        self.assertEqual(namespace["metadata"]["labels"].get("istio-injection"), "disabled")

    def test_pvc_is_read_write_once_and_shared_by_name(self) -> None:
        pvc = self.by_kind["PersistentVolumeClaim"][0]
        self.assertEqual(pvc["spec"]["accessModes"], ["ReadWriteOnce"])
        pvc_name = pvc["metadata"]["name"]

        scheduler = next(d for d in self.by_kind["Deployment"] if d["metadata"]["name"] == "scheduler")
        volumes = scheduler["spec"]["template"]["spec"]["volumes"]
        claim_names = {
            v["persistentVolumeClaim"]["claimName"] for v in volumes if "persistentVolumeClaim" in v
        }
        self.assertEqual(claim_names, {pvc_name})

    def test_scheduler_and_runner_pin_to_same_node_selector(self) -> None:
        """local-path StorageClass is node-local; scheduler and (dynamically
        created) runner Jobs must land on the same node as the PV."""
        scheduler = next(d for d in self.by_kind["Deployment"] if d["metadata"]["name"] == "scheduler")
        scheduler_node = scheduler["spec"]["template"]["spec"]["nodeSelector"]["kubernetes.io/hostname"]

        env = {e["name"]: e.get("value") for e in scheduler["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env.get("RUNNER_NODE_HOSTNAME"), scheduler_node)

    def test_scheduler_uses_dedicated_service_account(self) -> None:
        scheduler = next(d for d in self.by_kind["Deployment"] if d["metadata"]["name"] == "scheduler")
        self.assertEqual(scheduler["spec"]["template"]["spec"]["serviceAccountName"], "7mimi-scheduler")

    def test_configmap_generated_from_repo_config_yaml_files(self) -> None:
        config_map = self.by_kind["ConfigMap"][0]
        self.assertEqual(set(config_map["data"].keys()), {"roles.yaml", "policy.yaml", "schedules.yaml"})
        # And the generated name really is hash-suffixed (kustomize default
        # behavior), matching the "self-pod-lookup" resolution strategy
        # documented in kustomization.yaml / kubernetes_runner.py.
        self.assertTrue(config_map["metadata"]["name"].startswith("7mimi-agent-config-"))
        self.assertNotEqual(config_map["metadata"]["name"], "7mimi-agent-config")

    def test_scheduler_config_volume_mounts_generated_configmap(self) -> None:
        config_map_name = self.by_kind["ConfigMap"][0]["metadata"]["name"]
        scheduler = next(d for d in self.by_kind["Deployment"] if d["metadata"]["name"] == "scheduler")
        volumes = {v["name"]: v for v in scheduler["spec"]["template"]["spec"]["volumes"]}
        self.assertEqual(volumes["config"]["configMap"]["name"], config_map_name)

    def test_no_argocd_instance_label_hardcoded_anywhere(self) -> None:
        """ArgoCD stamps its own app.kubernetes.io/instance label at apply
        time; the manifests themselves must not hardcode one (would be
        redundant at best, and for the runner Job builder specifically,
        having Jobs mimic this label would pull them into ArgoCD's tracked
        set -- see kubernetes_runner.py)."""
        raw = (K8S_DIR / "scheduler.yaml").read_text(encoding="utf-8")
        self.assertNotIn("app.kubernetes.io/instance", raw)


@unittest.skipUnless(shutil.which("kubectl"), "kubectl not available")
class ArgoCdApplicationTest(unittest.TestCase):
    def setUp(self) -> None:
        path = REPO_ROOT / "deploy" / "argocd" / "application.yaml"
        self.assertTrue(path.exists(), f"missing {path}")
        self.app = yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_api_version_and_kind(self) -> None:
        self.assertEqual(self.app["apiVersion"], "argoproj.io/v1alpha1")
        self.assertEqual(self.app["kind"], "Application")

    def test_metadata_name_and_namespace(self) -> None:
        """This Application's identity (name/namespace) is what a
        cluster-management (private) app-of-apps entry adopts to match
        the object already live in the cluster; changing either here is
        a breaking change for that adoption and must be deliberate, not
        accidental."""
        self.assertEqual(self.app["metadata"]["name"], "7mimi-agent")
        self.assertEqual(self.app["metadata"]["namespace"], "argocd")

    def test_no_unexpected_top_level_or_metadata_fields(self) -> None:
        """Guards against fields (e.g. finalizers) creeping in that would
        change the live object's behavior on adoption/sync beyond the
        reviewed spec."""
        self.assertEqual(set(self.app.keys()), {"apiVersion", "kind", "metadata", "spec"})
        self.assertEqual(set(self.app["metadata"].keys()), {"name", "namespace"})

    def test_points_at_k8s_kustomize_directory(self) -> None:
        self.assertEqual(self.app["spec"]["source"]["path"], "deploy/k8s")

    def test_prune_is_disabled(self) -> None:
        """Deliberate: this namespace holds the Secret-backed proxy
        boundary and the only production scheduler instance, so accidental
        manifest deletion pruning live resources must not be automatic."""
        self.assertFalse(self.app["spec"]["syncPolicy"]["automated"]["prune"])

    def test_self_heal_is_enabled(self) -> None:
        self.assertTrue(self.app["spec"]["syncPolicy"]["automated"]["selfHeal"])


if __name__ == "__main__":
    unittest.main()
