"""Launch an ephemeral GitHub Actions runner on Modal with GPU support."""

import os
import string
import time
from random import choices

import modal
import requests

app = modal.App("github-actions-runner")

RUNNER_HOME = "/runner"
DEFAULT_RUNNER_VERSION = "2.322.0"
DEFAULT_BASE_IMAGE = "nvidia/cuda:12.4.0-devel-ubuntu22.04"


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


def wait_for_runner(
    repo: str,
    token: str,
    label: str,
    timeout: int = 120,
    interval: int = 5,
) -> bool:
    """Poll GitHub API until a runner with the given label appears online."""
    url = f"https://api.github.com/repos/{repo}/actions/runners"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        for runner in resp.json().get("runners", []):
            if label in [lbl["name"] for lbl in runner.get("labels", [])]:
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
    debug: bool = False,
):
    """Launch an ephemeral GitHub Actions runner on Modal.

    Args:
        repo: GitHub repo (owner/name)
        token: GitHub PAT with admin:org + repo scope
        gpu: Modal GPU type (e.g. T4, A10G, A100)
        timeout: Sandbox timeout in minutes
        base_image: Base Docker image
        runner_version: GitHub Actions runner version
        debug: Stream sandbox stdout/stderr
    """
    label = generate_label()
    print(f"Generated runner label: {label}")

    print(f"Requesting JIT config for {repo}...")
    jit_config = generate_jit_config(repo, token, label)

    print(f"Building runner image (base={base_image}, runner={runner_version})...")
    image = build_runner_image(base_image, runner_version)

    print(f"Creating sandbox (gpu={gpu}, timeout={timeout}m)...")
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
        app=app,
    )
    print(f"Sandbox created: {sandbox.object_id}")

    write_github_output("id", label)
    print(f"::set-output name=id::{label}")

    if debug:
        print("--- Streaming sandbox stdout ---")
        for chunk in sandbox.stdout:
            print(chunk, end="")

    print(f"Waiting for runner '{label}' to come online...")
    if wait_for_runner(repo, token, label):
        print(f"Runner '{label}' is ready. Downstream jobs can use: runs-on: {label}")
    else:
        print(f"WARNING: Runner '{label}' did not appear within polling timeout.")
        print("The sandbox may still be starting up. Check GitHub Actions runners page.")
