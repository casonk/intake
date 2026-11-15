"""deploy/deploy.sh: one setting chooses where untrusted receipts are parsed, and only k3s may touch a cluster.

The script runs against stub commands that record every call, so nothing here needs systemd, podman or a cluster,
which is the point: a developer without K3s must be able to run and test intake.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy" / "deploy.sh"
DAEMON = "intake-daemon.service"
RESTART = f"systemctl --user restart {DAEMON}"


def _stub(path: Path, log: Path, name: str, body: str = "") -> None:
    path.write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "{log}"\n{body}\n')
    path.chmod(0o755)


@pytest.fixture
def sandbox(tmp_path):
    """A PATH of recording stubs, a stub k3s directory, and private state and unit folders."""
    bin_dir, k3s, log = tmp_path / "bin", tmp_path / "k3s", tmp_path / "calls.log"
    bin_dir.mkdir()
    k3s.mkdir()
    for name, body in {
        "kubectl": "exit 99",
        "podman": "exit 99",
        "curl": "exit 22",
        "systemctl": 'case "$*" in *is-active*) echo inactive; exit 3 ;; esac',
    }.items():
        _stub(bin_dir / name, log, name, body)
    for step in ("build-image.sh", "apply.sh", "verify.sh", "bridge.sh"):
        _stub(k3s / step, log, step)
    shutil.copy(REPO / "deploy" / "k3s" / "env_value.py", k3s / "env_value.py")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "INTAKE_K3S_DIR": str(k3s),
        "INTAKE_DEPLOY_CONFIG": str(tmp_path / "deploy.local.env"),
        "INTAKE_UNITS_DIR": str(tmp_path / "units"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    env.pop("INTAKE_DEPLOY_MODE", None)
    return env, log, tmp_path


def run(env, *args):
    return subprocess.run(
        ["bash", str(DEPLOY), *args], env=env, capture_output=True, text=True, timeout=60
    )


def calls(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def names(log: Path) -> list[str]:
    return [c.split()[0] for c in calls(log)]


def dropin(tmp_path) -> Path:
    return tmp_path / "units" / f"{DAEMON}.d" / "20-extractor-sandbox.conf"


def _cluster_ready(tmp_path):
    log = tmp_path / "calls.log"
    _stub(tmp_path / "bin" / "curl", log, "curl")
    _stub(tmp_path / "bin" / "kubectl", log, "kubectl")


def _marker(tmp_path, value="k3s"):
    folder = tmp_path / "state" / "intake"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "deploy-mode").write_text(value + "\n")
    return folder / "deploy-mode"


def _with_dropin(tmp_path):
    path = dropin(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[Service]\nEnvironment=INTAKE_EXTRACTOR=sandbox\n")
    return path


def test_the_default_mode_is_dev_so_a_fresh_checkout_needs_no_cluster(sandbox):
    env, _, _ = sandbox
    done = run(env, "mode")
    assert done.returncode == 0 and done.stdout.strip() == "dev (from default)"


def test_the_mode_comes_from_flag_then_environment_then_config_then_default(sandbox):
    env, _, tmp = sandbox
    config = tmp / "deploy.local.env"
    config.write_text('INTAKE_DEPLOY_MODE="host"\n')
    from_config = run(env, "mode").stdout
    # the quoted config value is read the way systemd would read it
    assert from_config.startswith("host (from ")
    assert str(config) in from_config
    assert (
        run({**env, "INTAKE_DEPLOY_MODE": "k3s"}, "mode").stdout.strip() == "k3s (from environment)"
    )
    flagged = run({**env, "INTAKE_DEPLOY_MODE": "k3s"}, "--mode", "dev", "mode")
    assert flagged.stdout.strip() == "dev (from --mode)"


def test_an_unknown_mode_is_an_error_not_a_silent_fallback(sandbox):
    env, _, tmp = sandbox
    done = run(env, "--mode", "docker", "mode")
    assert done.returncode == 2 and "unknown deployment mode" in done.stderr
    (tmp / "deploy.local.env").write_text("INTAKE_DEPLOY_MODE=cloud\n")
    done = run(env, "mode")
    assert done.returncode == 2 and str(tmp / "deploy.local.env") in done.stderr


def test_a_config_file_that_cannot_be_read_is_an_error_not_a_quiet_default(sandbox):
    env, _, tmp = sandbox
    config = tmp / "deploy.local.env"
    config.write_text("INTAKE_DEPLOY_MODE=host\n")
    config.chmod(0)
    if os.access(config, os.R_OK):  # running as root: permissions do not apply
        pytest.skip("cannot make the file unreadable")
    done = run(env, "mode")
    assert done.returncode != 0
    assert "dev (from default)" not in done.stdout


def test_a_missing_command_is_a_usage_error(sandbox):
    env, _, _ = sandbox
    assert run(env).returncode == 2


@pytest.mark.parametrize("command", ["mode", "status", "verify", "down"])
def test_dev_never_touches_kubectl_podman_or_systemd(sandbox, command):
    env, log, _ = sandbox
    run(env, "--mode", "dev", command)
    assert [n for n in names(log) if n in {"kubectl", "podman", "systemctl"}] == []


def test_host_up_switches_the_daemon_back_to_in_process_parsing_and_never_touches_a_cluster(
    sandbox,
):
    env, log, tmp = sandbox
    present = _with_dropin(tmp)
    done = run(env, "--mode", "host", "up")
    assert done.returncode == 0, done.stderr
    # the sandbox drop-in is removed
    assert not present.exists()

    seen = calls(log)
    assert RESTART in seen
    assert seen.index("systemctl --user daemon-reload") < seen.index(RESTART)
    assert [n for n in names(log) if n in {"kubectl", "podman"} or n == "bridge.sh"] == []


def test_leaving_k3s_takes_the_bridge_down_scales_the_pod_to_zero_and_clears_the_marker(sandbox):
    env, log, tmp = sandbox
    _cluster_ready(tmp)
    _with_dropin(tmp)
    marker = _marker(tmp)
    done = run(env, "--mode", "host", "up")
    assert done.returncode == 0, done.stderr
    seen = calls(log)
    assert "bridge.sh off" in seen
    assert any(c.startswith("kubectl") and "--replicas=0" in c for c in seen)
    assert not dropin(tmp).exists() and not marker.exists()


def test_leaving_k3s_without_kubectl_still_restores_the_daemon_and_says_what_is_left(sandbox):
    env, log, tmp = sandbox
    _with_dropin(tmp)
    _marker(tmp)
    done = run({**env, "KUBECTL": "definitely-not-installed"}, "--mode", "host", "up")
    assert done.returncode == 0 and "keeps running, idle" in done.stderr
    # an idle pod is harmless; the daemon must still be restored
    assert not dropin(tmp).exists()


def test_k3s_refuses_without_kubectl_and_says_dev_and_host_do_not_need_it(sandbox):
    env, log, _ = sandbox
    done = run({**env, "KUBECTL": "definitely-not-installed"}, "--mode", "k3s", "up")
    assert done.returncode == 1
    assert (
        "k3s mode needs kubectl" in done.stderr and "dev and host modes need neither" in done.stderr
    )
    assert [n for n in names(log) if n in {"podman", "systemctl"} or n.endswith(".sh")] == []


def test_k3s_refuses_without_the_registry_and_changes_nothing(sandbox):
    env, log, tmp = sandbox
    done = run(env, "--mode", "k3s", "up")  # kubectl and podman exist (stubs); the curl stub fails
    assert done.returncode == 1 and "in-cluster registry" in done.stderr
    assert [n for n in names(log) if n.endswith(".sh") or n == "systemctl"] == []
    assert not dropin(tmp).exists()


def test_k3s_up_points_the_daemon_at_the_sandbox_only_after_the_bridge_is_up_and_verify_passes(
    sandbox,
):
    env, log, tmp = sandbox
    _cluster_ready(tmp)
    done = run(env, "--mode", "k3s", "up")
    assert done.returncode == 0, done.stderr
    seen = calls(log)
    order = [
        seen.index(c)
        for c in ("build-image.sh ", "apply.sh ", "bridge.sh on", "verify.sh ", RESTART)
    ]
    assert order == sorted(order), seen
    content = dropin(tmp).read_text()
    assert "Environment=INTAKE_EXTRACTOR=sandbox" in content
    assert "Environment=INTAKE_EXTRACTOR_URL=http://127.0.0.1:5455" in content
    assert "Managed by intake's deploy/deploy.sh" in content
    assert (tmp / "state" / "intake" / "deploy-mode").read_text().strip() == "k3s"


@pytest.mark.parametrize("failing", ["build-image.sh", "apply.sh"])
def test_a_failed_build_or_apply_changes_nothing_on_the_host(sandbox, failing):
    env, log, tmp = sandbox
    _cluster_ready(tmp)
    _stub(tmp / "k3s" / failing, log, failing, "exit 1")
    done = run(env, "--mode", "k3s", "up")
    assert done.returncode != 0
    assert not dropin(tmp).exists()
    assert not any(c.startswith("bridge.sh") for c in calls(log))
    assert RESTART not in calls(log)
    assert not (tmp / "state" / "intake" / "deploy-mode").exists()


def test_a_failed_bridge_or_verify_takes_the_bridge_down_and_never_touches_the_daemon(sandbox):
    env, log, tmp = sandbox
    _cluster_ready(tmp)
    _stub(tmp / "k3s" / "verify.sh", log, "verify.sh", "exit 1")
    done = run(env, "--mode", "k3s", "up")
    assert done.returncode != 0
    seen = calls(log)
    assert "bridge.sh on" in seen and "bridge.sh off" in seen
    # the daemon is not restarted for a sandbox that never became ready
    assert RESTART not in seen

    assert not dropin(tmp).exists() and not (tmp / "state" / "intake" / "deploy-mode").exists()


def test_a_failed_bridge_start_is_rolled_back_too(sandbox):
    env, log, tmp = sandbox
    _cluster_ready(tmp)
    _stub(tmp / "k3s" / "bridge.sh", log, "bridge.sh", '[ "$1" = on ] && exit 1 || exit 0')
    done = run(env, "--mode", "k3s", "up")
    assert done.returncode != 0
    assert "bridge.sh off" in calls(log)
    # nothing is verified through a bridge that did not come up
    assert "verify.sh " not in calls(log)
    assert not dropin(tmp).exists()


def test_a_daemon_that_will_not_restart_is_put_back_to_in_process_parsing(sandbox):
    env, log, tmp = sandbox
    _cluster_ready(tmp)
    body = 'case "$*" in *restart*) exit 1 ;; *is-active*) echo inactive; exit 3 ;; esac'
    _stub(tmp / "bin" / "systemctl", log, "systemctl", body)
    done = run(env, "--mode", "k3s", "up")
    assert done.returncode != 0
    # the drop-in must not outlive a failed switch
    assert not dropin(tmp).exists()

    assert "bridge.sh off" in calls(log)
    assert not (tmp / "state" / "intake" / "deploy-mode").exists()


def test_k3s_down_is_host_up(sandbox):
    env, log, tmp = sandbox
    _cluster_ready(tmp)
    _with_dropin(tmp)
    _marker(tmp)
    done = run(env, "--mode", "k3s", "down")
    assert done.returncode == 0, done.stderr
    assert "bridge.sh off" in calls(log) and not dropin(tmp).exists()


def test_the_image_build_refuses_uncommitted_source_unless_told_otherwise(tmp_path):
    repo = tmp_path / "repo"
    (repo / "deploy" / "k3s").mkdir(parents=True)
    (repo / "intake").mkdir()
    shutil.copy(
        REPO / "deploy" / "k3s" / "build-image.sh", repo / "deploy" / "k3s" / "build-image.sh"
    )
    (repo / "deploy" / "k3s" / "Containerfile").write_text("FROM scratch\n")
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        (repo / name).write_text("x\n")
    (repo / "intake" / "ocr.py").write_text("x = 1\n")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    (repo / "intake" / "ocr.py").write_text("x = 2\n")

    log = tmp_path / "calls.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir / "podman", log, "podman")
    crew = tmp_path / "crew" / "deploy" / "k3s"
    crew.mkdir(parents=True)
    _stub(crew / "push-image.sh", log, "push-image.sh")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CREW_CHIEF_DIR": str(tmp_path / "crew"),
    }
    script = repo / "deploy" / "k3s" / "build-image.sh"

    refused = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert refused.returncode == 1 and "uncommitted changes" in refused.stderr
    assert "podman" not in names(log)

    allowed = subprocess.run(
        ["bash", str(script)], env={**env, "ALLOW_DIRTY": "1"}, capture_output=True, text=True
    )
    assert allowed.returncode == 0, allowed.stderr
    assert "podman" in names(log)


def test_host_scripts_use_only_python_flags_that_exist_on_the_oldest_supported_python():
    # `python3 -P` is Python 3.11+; -I (isolated) exists everywhere and keeps the script directory off sys.path.
    for path in (REPO / "deploy").rglob("*.sh"):
        for line in path.read_text().splitlines():
            if "python3 -P" in line:
                assert " exec " in line, f"{path.name}: {line.strip()}"


def test_the_local_config_is_gitignored_and_the_example_and_docs_cover_every_mode(sandbox):
    env, _, _ = sandbox
    assert "config/deploy.local.env" in (REPO / ".gitignore").read_text()
    example = REPO / "config" / "deploy.example.env"
    done = run({**env, "INTAKE_DEPLOY_CONFIG": str(example)}, "mode")
    assert done.returncode == 0 and done.stdout.startswith("dev (from ")
    docs = (REPO / "docs" / "deployment.md").read_text()
    for needle in (
        "`dev`",
        "`host`",
        "`k3s`",
        "Switching",
        "only the `k3s` mode needs a",
        "deploy/deploy.sh",
    ):
        assert needle in docs, needle
    assert "docs/deployment.md" in (REPO / "README.md").read_text()


def test_the_restore_trap_fires_for_failures_inside_functions():
    # Without `set -E` bash does not inherit an ERR trap into functions, so a failure inside `bridge` or
    # `install_dropin` skipped the restore and left the daemon pointed at a sandbox that never came up.
    text = DEPLOY.read_text()
    assert "set -eEuo pipefail" in text
    assert "trap restore ERR" in text
