# Releasing LAMCP

## One-time setup

### 1. Configure PyPI trusted publisher (OIDC)

On the PyPI side, before the first release:

1. Create the project page at <https://pypi.org/manage/account/publishing/>.
2. Add a **pending publisher** with:
    * PyPI Project Name: `lamcp`
    * Owner: `gramaziokohler`
    * Repository name: `lamcp`
    * Workflow name: `release.yml`
    * Environment name: `pypi`

This lets the GitHub Actions workflow publish to PyPI without storing a
token in the repo (OIDC short-lived credentials instead).

### 2. Create the `pypi` environment in GitHub

In the repo settings → Environments → New environment → `pypi`.

Optionally add deployment protection rules (required reviewers, branch
restrictions). The `publish-pypi` job in `.github/workflows/release.yml`
runs in this environment.

## Cutting a release

1. Update `CHANGELOG.md`:
    * Rename `## Unreleased` to `## [X.Y.Z] - YYYY-MM-DD`.
    * Add a new empty `## Unreleased` section above it.

2. Bump the version in `pyproject.toml` (`project.version`).

3. Commit and push the bump:

   ```bash
   git add pyproject.toml CHANGELOG.md
   git commit -m "Bump version to X.Y.Z"
   git push
   ```

4. Tag and push:

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

5. Watch the `release` workflow under Actions. It:
    * Builds the Python sdist + wheel
    * Builds `Lamcp_Bridge.ghuser` on Windows via
      `compas-dev/compas-actions.ghpython_components`
    * Publishes the wheel + sdist to PyPI via OIDC
    * Creates a GitHub release with auto-generated notes, attaching the
      wheel, sdist, and `Lamcp_Bridge.ghuser` as downloadable assets

## Versioning

SemVer. `0.x` is unstable — API can break in any minor. `1.0` is the
commitment to backward compatibility.
