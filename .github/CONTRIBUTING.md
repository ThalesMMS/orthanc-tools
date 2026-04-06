# Contributing

## Before You Start

- Search existing issues and pull requests before opening a new one.
- Keep changes scoped. Avoid mixing repo hygiene, feature work, and unrelated refactors in one PR.
- Update docs when behavior, entrypoints, defaults, or safety guidance change.

## Development Flow

1. Fork or branch from the current default branch.
2. Make the smallest coherent change that solves the problem.
3. Run the local validation commands.
4. Open a pull request with a clear summary and testing notes.

## Local Validation

Run these commands before opening a PR:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m compileall src scripts orthanc_tools
find scripts deploy -type f \( -name '*.sh' -o -name '*.command' \) -print0 | xargs -0 -n1 bash -n
docker compose -f deploy/docker/compose.yaml config
```

## Pull Request Expectations

- Explain the user-facing or operator-facing impact.
- Mention any documentation changes.
- Mention any security-relevant change, especially around Orthanc access controls or exposed services.
- Keep PR descriptions decision-complete enough for reviewers to validate intent quickly.

## Documentation Expectations

Update relevant docs when you change:

- public CLI or script entrypoints
- deployment behavior
- security posture or examples
- support or contribution policy

## Security Reports

Do not open a public issue for undisclosed vulnerabilities.

Use GitHub Security Advisories or the repository's private vulnerability reporting flow for sensitive reports. See [`SECURITY.md`](SECURITY.md).
