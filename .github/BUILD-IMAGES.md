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
for `linux/amd64` (ECS `X86_64`); each successful build uploads an OCI
archive named `image-<service>-<commit SHA>`, retained for one day. This is a
temporary handoff for later CI stages; it is not a published ECR image, a
signature, or a vulnerability scan. Before those stages are connected, their
handling of the OCI archive and image digest must be verified together.

This selector currently covers services with `build.gradle.kts`; image builds
for non-Gradle services need their own selection contract before being added.
Each service owner must also verify that its Dockerfile meets the documented
security and `linux/amd64` requirements.

Run the selection tests locally:

```sh
python -m unittest discover -s .github/scripts/tests -v
```
