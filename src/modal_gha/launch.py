"""Launch an ephemeral GitHub Actions runner on Modal with GPU support."""

import os
import string
import time
from random import choices

import modal
import requests

app = modal.App("github-actions-runner")

RUNNER_HOME = "/runner"
DEFAULT_RUNNER_VERSION = "2.332.0"
DEFAULT_BASE_IMAGE = "nvidia/cuda:12.4.0-devel-ubuntu22.04"

AVAILABLE_GPUS = ["T4", "L4", "A10G", "L40S", "A100", "H100"]


def build_runner_image(
    base_image: str = DEFAULT_BASE_IMAGE,
    runner_version: str = DEFAULT_RUNNER_VERSION,
) -> modal.Image:
    """Build a Modal image with the GitHub Actions runner binary pre-installed."""
    return (
        modal.Image.from_registry(base_image, add_python="3.12")
        .apt_install("curl", "git", "jq", "ca-certificates", "sudo")
        .run_commands(
            f"mkdir -p {RUNNER_HOME}",
            f"curl -sL https://github.com/actions/runner/releases/download/v{runner_version}/actions-runner-linux-x64-{runner_version}.tar.gz"
            f" | tar xz -C {RUNNER_HOME}",
            f"bash {RUNNER_HOME}/bin/installdependencies.sh",
        )
        .env({"RUNNER_ALLOW_RUNASROOT": "1"})
    )


def generate_label() -> str:
    """Generate a unique runner label like 'modal-a1b2c3d4'."""
    suffix = "".join(choices(string.ascii_lowercase + string.digits, k=8))
    return f"modal-{suffix}"


def generate_jit_config(
    repo: str,
    token: str,
    label: str,
) -> str:
    """Register a just-in-time runner and return the encoded JIT config.

    Uses POST /repos/{repo}/actions/runners/generate-jitconfig
    """
    url = f"https://api.github.com/repos/{repo}/actions/runners/generate-jitconfig"
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={
            "name": label,
            "runner_group_id": 1,
            "labels": [label],
        },
    )
    resp.raise_for_status()
    return resp.json()["encoded_jit_config"]


def write_github_output(name: str, value: str) -> None:
    """Append name=value to $GITHUB_OUTPUT (no-op outside GHA)."""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as f:
            f.write(f"{name}={value}\n")


def gha_tags() -> dict[str, str]:
    """Build sandbox tags from GitHub Actions environment variables."""
    tag_vars = {
        "repo": "GITHUB_REPOSITORY",
        "sha": "GITHUB_SHA",
        "ref": "GITHUB_REF",
        "run_id": "GITHUB_RUN_ID",
        "run_number": "GITHUB_RUN_NUMBER",
        "workflow": "GITHUB_WORKFLOW",
        "actor": "GITHUB_ACTOR",
    }
    tags = {}
    for key, env_var in tag_vars.items():
        val = os.environ.get(env_var)
        if val:
            tags[key] = val
    return tags


def wait_for_runner(
    repo: str,
    token: str,
    label: str,
    sandbox: modal.Sandbox,
    timeout: int = 120,
    interval: int = 5,
) -> bool:
    """Poll GitHub API until a runner with the given label appears online.

    Also monitors the sandbox — if it exits early, raises immediately.
    """
    url = f"https://api.github.com/repos/{repo}/actions/runners"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    deadline = time.time() + timeout
    while time.time() < deadline:
        poll = sandbox.poll()
        if poll is not None:
            print("Sandbox exited early! Collecting logs...")
            for chunk in sandbox.stdout:
                print(chunk, end="")
            for chunk in sandbox.stderr:
                print(chunk, end="")
            raise RuntimeError(f"Sandbox exited with code {poll} before runner came online")

        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        for runner in resp.json().get("runners", []):
            runner_labels = [lbl["name"] for lbl in runner.get("labels", [])]
            if label in runner_labels and runner.get("status") == "online":
                print(f"Runner '{label}' is online (id={runner['id']})")
                return True
        time.sleep(interval)
    return False


@app.local_entrypoint()
def main(
    repo: str,
    token: str,
    gpu: str = "A10G",
    timeout: int = 60,
    base_image: str = DEFAULT_BASE_IMAGE,
    runner_version: str = DEFAULT_RUNNER_VERSION,
):
    """Launch an ephemeral GitHub Actions runner on Modal.

    Args:
        repo: GitHub repo (owner/name)
        token: GitHub PAT with admin:org + repo scope
        gpu: Modal GPU type (T4, L4, A10G, L40S, A100, H100)
        timeout: Sandbox timeout in minutes
        base_image: Base Docker image
        runner_version: GitHub Actions runner version
    """
    label = generate_label()
    print(f"Generated runner label: {label}")

    print(f"Requesting JIT config for {repo}...")
    jit_config = generate_jit_config(repo, token, label)

    print(f"Building runner image (base={base_image}, runner={runner_version})...")
    image = build_runner_image(base_image, runner_version)

    print(f"Creating sandbox (gpu={gpu}, timeout={timeout}m)...")
    sb_app = modal.App.lookup("modal-gha-runner", create_if_missing=True)
    sandbox = modal.Sandbox.create(
        "bash",
        "-c",
        f"{RUNNER_HOME}/run.sh --jitconfig $JIT_CONFIG",
        image=image,
        gpu=gpu,
        timeout=timeout * 60,
        encrypted_ports=[],
        secrets=[
            modal.Secret.from_dict({"JIT_CONFIG": jit_config}),
        ],
        app=sb_app,
    )
    print(f"Sandbox created: {sandbox.object_id}")

    tags = {"label": label, "gpu": gpu, **gha_tags()}
    sandbox.set_tags(tags)
    print(f"Tags: {tags}")

    write_github_output("id", label)

    print(f"Waiting for runner '{label}' to come online...")
    if wait_for_runner(repo, token, label, sandbox):
        print(f"Runner '{label}' is ready. Downstream jobs can use: runs-on: {label}")
    else:
        raise RuntimeError(f"Runner '{label}' did not come online within polling timeout")
