# modal-gha

Ephemeral GPU runners on [Modal] for GitHub Actions. Launches a Modal Sandbox with the GitHub Actions runner binary, using JIT (just-in-time) runner config for zero-config ephemeral registration.

## Architecture

1. `launch.py` generates a unique runner label, requests JIT config from the GitHub API, builds a Modal image with the runner binary baked in, and creates a Sandbox with a GPU
2. The Sandbox runs `run.sh --jitconfig ...` which connects to GitHub and picks up jobs matching the label
3. Downstream GHA jobs use `runs-on: ${{ needs.modal.outputs.id }}` to target the runner
4. The runner self-terminates after the job completes (JIT runners are inherently ephemeral)

## Key files

- `src/modal_gha/launch.py` — Main entrypoint (run via `modal run`)
- `.github/workflows/runner.yml` — Reusable workflow (callers use `workflow_call`)
- `.github/workflows/e2e-test.yml` — Self-test: launches T4 runner, runs `nvidia-smi`

## Available GPUs

T4, L4, A10G, L40S, A100, H100

## Secrets required

- `GH_SA_TOKEN` — GitHub PAT with repo admin scope (for JIT runner registration)
- `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` — Modal API credentials

## Usage from another repo

```yaml
jobs:
  modal:
    uses: Open-Athena/modal-gha/.github/workflows/runner.yml@main
    secrets:
      GH_SA_TOKEN: ${{ secrets.GH_SA_TOKEN }}
      MODAL_TOKEN_ID: ${{ secrets.MODAL_TOKEN_ID }}
      MODAL_TOKEN_SECRET: ${{ secrets.MODAL_TOKEN_SECRET }}
    with:
      gpu: "T4"
      timeout: "30"

  my-job:
    needs: modal
    runs-on: ${{ needs.modal.outputs.id }}
    steps:
      - run: nvidia-smi
```

## Related projects

- [ec2-gha] — Same pattern on AWS EC2
- [lambda-gha] — Same pattern on Lambda Labs
- [cloud-gha] — Unified dispatch layer (WIP) across providers

[Modal]: https://modal.com
[ec2-gha]: https://github.com/Open-Athena/ec2-gha
[lambda-gha]: https://github.com/Open-Athena/lambda-gha
[cloud-gha]: https://github.com/Open-Athena/cloud-gha
