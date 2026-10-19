# Repository instructions

## North star

Make the default Antoine/Gazebo/NATS profile a reproducible local drone-security research lab. Prefer complete, bounded experiment paths with independent evidence and clear impact limits over broad scenario relabeling. Only call an attack path active when the current launcher exposes its interface; keep legacy ArduPilot/MAVLink exercises clearly separated, and report inconclusive or blocked results as such.

## Python

- Use `uv` whenever you run a Python command from the host development environment. Prefer `uv run --no-project python ...` for repository utilities that use only the standard library; use `uv run --project <path> python ...` when the target project has a `pyproject.toml`.
- Do not invoke host `python`, `python3`, `pip`, or `pip3` directly, and do not install host Python packages globally. Add dependencies to the relevant project configuration and run them through `uv`.
- Python processes that run inside a Docker image should use that image's configured interpreter and dependencies; do not add host `uv` requirements to container entrypoints.
