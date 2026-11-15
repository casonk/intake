"""The K3s extractor sandbox: it can reach nothing, hold nothing, and only the node can reach it."""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy" / "k3s"
MANIFESTS = DEPLOY / "manifests"


def docs(name: str) -> list[dict]:
    return list(yaml.safe_load_all((MANIFESTS / name).read_text()))


def test_the_namespace_enforces_the_restricted_pod_security_standard():
    (namespace,) = docs("00-namespace.yaml")
    assert namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"


def test_only_the_node_may_connect_and_the_pod_may_connect_to_nothing():
    (policy,) = docs("30-network-policy.yaml")
    spec = policy["spec"]
    assert spec["ingress"] == [
        {"fromEntities": ["host"], "toPorts": [{"ports": [{"port": "5455", "protocol": "TCP"}]}]}
    ]
    assert spec["egressDeny"] == [{"toEntities": ["all"]}]
    # no allow rule can open a path the deny closes
    assert "egress" not in spec


def test_the_pod_holds_none_of_your_files_no_secrets_and_no_environment():
    deployment, service = docs("20-deployment.yaml")
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    # the only volume is a bounded scratch directory
    assert [v["name"] for v in pod["volumes"]] == ["scratch"]
    scratch = pod["volumes"][0]
    assert set(scratch) == {"name", "emptyDir"} and scratch["emptyDir"]["sizeLimit"]
    assert [m["mountPath"] for m in container["volumeMounts"]] == ["/tmp"]
    assert "env" not in container and "envFrom" not in container
    assert pod["automountServiceAccountToken"] is False
    assert "hostPath" not in str(pod) and "secret" not in str(pod).lower().replace(
        "securitycontext", ""
    )
    assert service["spec"]["type"] == "ClusterIP" and service["spec"]["clusterIP"] == "10.43.0.61"


def test_the_pod_runs_unprivileged_as_a_uid_that_owns_nothing_of_yours():
    deployment, _ = docs("20-deployment.yaml")
    pod = deployment["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    # not the owner's uid (1000)
    assert pod["securityContext"]["runAsUser"] == 10001

    container = pod["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert container["image"] == "localhost:5000/intake-extractor:k3s"
    # a hostile file must hit a memory ceiling
    assert container["resources"]["limits"]["memory"]

    assert pod["terminationGracePeriodSeconds"] <= 10


def test_the_image_carries_the_parsers_and_none_of_your_data_or_configuration():
    text = (DEPLOY / "Containerfile").read_text()
    assert "tesseract-ocr" in text and "ffmpeg" in text
    assert "USER 10001:10001" in text
    for private in ("config", "data", "settings.toml", ".env", "reports"):
        assert f"COPY {private}" not in text, private
    assert "COPY intake ./intake" in text
    build = (DEPLOY / "build-image.sh").read_text()
    assert "config.local" not in build and "settings.toml" not in build


def test_the_bridge_stays_up_through_a_burst_and_listens_on_loopback_only():
    service = (DEPLOY / "systemd" / "intake-extractor.service").read_text()
    exec_line = next(line for line in service.splitlines() if line.startswith("ExecStart="))
    # `--exit-idle-time=0` started one service per connection and tripped systemd's start-rate limit.
    assert "--exit-idle-time" not in exec_line
    assert "StartLimitIntervalSec=0" in service
    assert "10.43.0.61:5455" in exec_line
    assert (
        "ListenStream=127.0.0.1:5455"
        in (DEPLOY / "systemd" / "intake-extractor.socket").read_text()
    )


def test_apply_creates_the_namespace_and_policy_before_the_pod_and_needs_nothing_from_the_host():
    text = (DEPLOY / "apply.sh").read_text()
    assert (
        text.index("00-namespace.yaml")
        < text.index("30-network-policy.yaml")
        < text.index("20-deployment.yaml")
    )
    for absent in (
        "create secret",
        "chcon",
        "prepare-host",
        "--from-env-file",
        "local PersistentVolume",
    ):
        assert absent not in text, absent
    # there is no host folder to label
    assert not (DEPLOY / "prepare-host.sh").exists()


def test_verify_cannot_report_success_without_running_its_checks_and_drives_real_documents():
    text = (DEPLOY / "verify.sh").read_text()
    assert "exit 2" in text and "nothing was verified" in text
    assert "could not run the probe" in text
    for needle in (
        "TOTAL 12.34",
        "image-only PDF",
        "decode-bad",
        "seq 1 60",
        "xargs -P 20",
        "the bridge socket died",
    ):
        assert needle in text, needle
    assert "png-text=True pdf-text=True png-preview=True decode-good=True decode-bad=False" in text
    assert text.rstrip().endswith("exit $fail")


def test_verify_waits_for_the_previous_peer_pod_to_be_gone_before_creating_the_next():
    # A second run within a minute raced the first run's pod while it was still terminating.
    text = (DEPLOY / "verify.sh").read_text()
    assert text.index("--wait=true") < text.index("kubectl -n default run")
    assert "peer test did not run" in text


def test_the_scripts_fail_fast_and_use_no_sudo():
    for name in ("apply.sh", "bridge.sh", "build-image.sh"):
        assert "set -euo pipefail" in (DEPLOY / name).read_text()
    for name in ("apply.sh", "bridge.sh", "build-image.sh", "verify.sh"):
        code = "\n".join(
            line
            for line in (DEPLOY / name).read_text().splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "sudo " not in code, name
