# Service image builds

`build-images.yml` selects Gradle services changed by a push to `qa`. A change
to shared Gradle configuration, libraries, or the workflow selects all Gradle
services. `sample-service` is excluded. Manual and reusable invocations accept
a service directory such as `identity-service` or `ALL-SERVICES`.

Each service owns `services/<service>/Dockerfile`. The build context is the
repository root, so the Dockerfile and its `Dockerfile.dockerignore` should
reflect the files it needs. `compose.yml` remains the local orchestration
configuration; the workflow does not use it to build images.

`identity-service` downloads Gradle and its dependencies in a separate layer,
before copying the sources. That layer is reused until a Gradle build file,
`gradle/`, `build-logic/` or a base image changes. The `build-files` stage and
the `downloadDependencies` task (from `mentora.servicio`) make this possible;
use the same pattern in other service Dockerfiles. In GitHub Actions the layers
are stored in the Actions cache, one scope per service. The first build, or one
after a cache eviction, still downloads everything. Base images are pinned by
digest, so their updates must be made explicitly.

The workflow reports selected services without a Dockerfile in the summary
and as a warning, but their absence does not fail the workflow. Services that
do have a Dockerfile are built only after the reusable CI workflow succeeds
for all selected services on the same commit. Shared changes run the full CI
including shared libraries; other changes test only selected services. If no
services are selected, both tests and builds are skipped. Images are built
for `linux/amd64` (ECS `X86_64`); each image that passes the security scan is
uploaded as an OCI archive named `image-<service>-<commit SHA>`, retained for
one day. This is a temporary handoff for later CI stages; it is not a published
ECR image or a signature. Before those stages are connected, their handling of
the OCI archive and image digest must be verified together.

## Security scan

`.github/scripts/scan-security.py` runs Trivy from its official container image,
pinned by digest. To update Trivy, review the release and its security
advisories, then change the version and the digest together.

| Gate | Where | Blocks |
| --- | --- | --- |
| Repository secrets | `ci.yml` (pull requests and reusable calls) | Any secret in the files tracked by Git |
| Image vulnerabilities | `build-images.yml`, before the archive is uploaded | `HIGH` or `CRITICAL` with a fix available |
| Image secrets | Same step, including the image configuration | Any secret |

The gates fail closed: if Trivy cannot finish, the job fails. The vulnerability
database changes every day, so an image that passed can be blocked later.
`ci.yml` also runs `selftest`, which gives both gates input they must block.

To consume the OCI archive, the script extracts it, checks that its root digest
is the one reported by the build, verifies the SHA-256 of the manifests,
configuration and layers, requires a single `linux/amd64` image and scans that
manifest. The archive is not rebuilt or modified: the same file is uploaded
afterwards. The reports, including the archive checksum and the scanned manifest
digest in `image-identity.txt`, are kept for 14 days (7 days for the repository
report). Publication must keep that manifest digest.

### Exceptions

False positives or temporarily accepted risks go in
`.github/trivy/exceptions.json`, through a reviewed pull request:

```json
{
  "vulnerabilities": [
    {
      "id": "CVE-0000-00000",
      "statement": "Why it is accepted and the follow-up task (MPC-NNN)",
      "expired_at": "2027-01-31"
    }
  ],
  "secrets": []
}
```

- `id`, `statement` (one line) and `expired_at` are required. `paths` is
  optional; without it the exception applies to every file, so use it whenever
  possible.
- `expired_at` can be at most 90 days after the day of the scan. From that date
  the entry is not applied and the finding blocks again. Renewing it needs a
  change to this file.
- A missing field, an unknown field, invalid JSON or a longer duration stops the
  scan with an error.
- Every entry, active or expired, is printed in the log and the job summary and,
  when reports are requested, saved in `exceptions.txt`. In GitHub Actions,
  active entries also raise a warning. `selftest` never uses exceptions.

These checks do not decide whether an exception is justified, and a pull request
can also change the script or the workflows. Review every change to them. A real
secret is never ignored: revoke or rotate it first, because removing it from the
files does not remove it from the Git history.

Limits: the repository scan covers the current files, not the Git history. By
default Trivy skips secrets in tests, examples and Markdown;
`.github/trivy/secret.yaml` enables those places again. A secret scanner only
finds the patterns that its rules know.

Run the scans locally (they need Docker and download Trivy and its databases the
first time):

```sh
python .github/scripts/scan-security.py selftest
python .github/scripts/scan-security.py repo
docker buildx build --platform linux/amd64 -f services/identity-service/Dockerfile --output type=oci,dest=../identity-service.tar .
python .github/scripts/scan-security.py image ../identity-service.tar
```

The archive is written outside the repository, so it cannot be committed or
sent back into the build context.

This selector currently covers services with `build.gradle.kts`; image builds
for non-Gradle services need their own selection contract before being added.
Each service owner must also verify that its Dockerfile meets the documented
security and `linux/amd64` requirements.

Run the script tests locally:

```sh
python -m unittest discover -s .github/scripts/tests -v
```
