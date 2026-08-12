# Repository Guidelines

## Project Structure & Module Organization

`fieldpilot/` contains the Python 3.12 application. Domain packages such as `safety/`, `events/`, `backend/`, and `notifications/` feed the shared detection-to-alert pipeline; keep integrations within that path instead of calling the dashboard directly. `tests/` mirrors backend behavior with pytest modules named `test_*.py`. The Next.js manager UI lives in `frontend/src/`, while the Flutter worker client is under `worker_app/lib/` with tests in `worker_app/test/`. Operational scripts are in `scripts/`, configuration is in `config.yaml`, and project documentation is in `docs/`.

## Build, Test, and Development Commands

- `make setup` installs Python/server/dev dependencies with `uv` and frontend packages with npm.
- `make doctor` checks Python, Docker, camera, and local tooling.
- `make run-all` starts infrastructure, backend (`:8100`), edge service (`:8000`), and dashboard (`:3000`); use `make stop-all` to stop them.
- `make edge-synthetic` runs the vision path without camera hardware.
- `make test` runs the backend pytest suite; `make lint` runs Ruff and ESLint.
- `make test-frontend && make frontend-build` type-checks and builds the dashboard.
- From `worker_app/`, run `flutter analyze` and `flutter test`.

## Coding Style & Naming Conventions

Use four spaces and type hints in Python. Ruff enforces Python 3.12 rules (`E`, `F`, `I`, `UP`, `B`) with a 100-character formatting target. Use `snake_case` for modules/functions and `PascalCase` for classes. Frontend TypeScript is strict: use PascalCase component files, `useX` hook names, the `@/` import alias, ESLint, and Prettier (`npm run format:check`). Follow the additional version-specific rules in `frontend/AGENTS.md`.

## Testing Guidelines

Add focused regression tests beside related coverage. Prefer hermetic pytest fixtures, `tmp_path`, the in-memory event bus, and SQLite; tests must not require cameras, cloud services, or downloaded models. Name Flutter tests `*_test.dart`. Run the relevant focused test first, then the full lint and test commands before review.

## Commit & Pull Request Guidelines

History uses short, imperative, sentence-style subjects, for example `Require authentication for administrative routes`. Keep each commit scoped and explain security or architecture consequences in its body. Pull requests should summarize behavior, list verification commands, link issues, and include screenshots or recordings for UI changes. Call out configuration, schema, API, or model-weight impacts explicitly.

## Security & Configuration

Never commit credentials, tokens, worker media, local databases, or model weights. Override nested settings with `FIELDPILOT_<SECTION>__<KEY>` variables. Preserve authentication and role checks, and remember that the edge service on port 8000 is development-only and unauthenticated.
