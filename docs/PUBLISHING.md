# Publishing hypeman-social to PyPI

The release workflow (`.github/workflows/release.yml`) publishes to PyPI using
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/) — OIDC-based,
no long-lived API token stored in GitHub secrets. It runs automatically when a
GitHub release is published.

> **Status:** the one-time setup below is complete — `hypeman-social` 0.1.0
> shipped via this pipeline on 2026-09-04
> ([pypi.org/project/hypeman-social](https://pypi.org/project/hypeman-social/)).
> For subsequent releases, jump to [Every release after that](#every-release-after-that).

## One-time setup

Do these once, in order:

### 1. Create the PyPI project's trusted publisher

Because the project doesn't exist on PyPI yet, use a *pending* publisher:

1. Log in to [pypi.org](https://pypi.org) (create an account if needed —
   enable 2FA, PyPI requires it for new projects).
2. Go to **Your account → Publishing** ([pypi.org/manage/account/publishing](https://pypi.org/manage/account/publishing/)).
3. Under **Add a new pending publisher**, choose **GitHub** and fill in:
   - **PyPI project name:** `hypeman-social`
   - **Owner:** `ChiefGyk3D`
   - **Repository name:** `hypeman`
   - **Workflow name:** `release.yml`
   - **Environment name:** `pypi`
4. Save. The first successful publish claims the name and converts the pending
   publisher into a regular one.

### 2. Create the GitHub environment

1. In the `hypeman` repo: **Settings → Environments → New environment**,
   named exactly `pypi`.
2. (Recommended) Add yourself as a required reviewer so a human approves
   every publish, and restrict deployment branches to `main`.

### 3. Cut a release

1. Bump `version` in `pyproject.toml` (e.g. `0.1.0`) on `main`.
2. Tag and release:

   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```

3. On GitHub: **Releases → Draft a new release**, pick the `v0.1.0` tag,
   write the notes, **Publish release**.
4. The workflow builds the sdist and wheel, verifies the tag matches
   `pyproject.toml` (a `v0.1.0` tag must ship version `0.1.0`), and publishes.
5. Verify: `pip install hypeman-social` from a clean environment, and check
   [pypi.org/project/hypeman-social](https://pypi.org/project/hypeman-social/).

## Every release after that

Bump the version, merge to `main`, tag `vX.Y.Z`, publish the GitHub release.
That's the whole procedure.

## Versioning

Semantic versioning:

- **Patch** (`0.1.1`) — bug fixes, no API change.
- **Minor** (`0.2.0`) — new platforms, providers, or profiles; existing APIs unchanged.
- **Major** (`1.0.0`) — breaking changes to public APIs the daemons import.

While the version is `0.x`, minor bumps may include breaking changes; the
daemons pin `hypeman-social>=0.1,<0.2` style ranges for that reason.

## TestPyPI dry run (optional)

To rehearse without touching the real index, add a pending publisher on
[test.pypi.org](https://test.pypi.org/manage/account/publishing/) with the same
values, then temporarily point the publish step at TestPyPI:

```yaml
      - name: Publish to TestPyPI
        uses: pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33 # v1.14.2
        with:
          repository-url: https://test.pypi.org/legacy/
```
