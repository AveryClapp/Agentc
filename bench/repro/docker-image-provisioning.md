# Docker image provisioning constraints

Primary-source check: 2026-10-12. Engine implementation links are pinned to
[Docker 29.9.0](https://github.com/moby/moby/releases/tag/docker-v29.9.0), released
2026-10-08; SDK links are pinned to docker-py 7.2.0, matching the installed
grader environment. These findings describe API behavior, not a live Docker
mutation test.

## Immutable pulls and completion

Pull `repository@sha256:<digest>` with an explicit platform. A digest pins the
registry content; a tag can move. Shared layers need not be downloaded again.
An image ID and a registry manifest digest are different kinds of identifiers;
retain both the exact requested reference and the inspected local identity.
[Docker pull documentation](https://docs.docker.com/reference/cli/docker/image/pull/)
explains digest pinning and layer sharing; the
[containerd inspect implementation](https://github.com/moby/moby/blob/docker-v29.9.0/daemon/containerd/image_inspect.go)
shows that identity and selected-platform reporting depend on the backend.

The Engine's `POST /images/create` API can pull **all repository tags** when no
tag or digest is provided. Always provide an explicit immutable reference;
never use repository-wide pulling for task provisioning. This endpoint exposes
no per-pull byte budget or unpacked-size cap.
[Engine API specification](https://github.com/moby/moby/blob/docker-v29.9.0/api/swagger.yaml#L10104-L10208)

After streaming starts, the image route can report failure inside the JSON
stream while the HTTP status remains successful. Treat streamed `error` or
`errorDetail` as failure and inspect the final requested reference before
recording success.
[Image API route](https://github.com/moby/moby/blob/docker-v29.9.0/daemon/server/router/image/image_routes.go#L157-L165)

## Cancellation stops work, but does not roll back storage

Docker documents that terminating `docker pull`, or cutting its client-daemon
connection, cancels the Engine operation. Current source preserves that path:
the server derives its handler context from the HTTP request, the image route
passes it to the pull backend, and both storage backends propagate it into the
actual pull.
[Docker cancellation documentation](https://docs.docker.com/reference/cli/docker/image/pull/#cancel-a-pull),
[HTTP context](https://github.com/moby/moby/blob/docker-v29.9.0/daemon/server/server.go#L49-L90),
[image route](https://github.com/moby/moby/blob/docker-v29.9.0/daemon/server/router/image/image_routes.go#L107-L115),
[containerd pull](https://github.com/moby/moby/blob/docker-v29.9.0/daemon/containerd/image_pull.go#L249-L257),
[legacy pull](https://github.com/moby/moby/blob/docker-v29.9.0/daemon/images/image_pull.go#L75-L119)

In docker-py 7.2.0, `api.pull` explicitly sends `timeout=None` and returns an
ordinary generator for `stream=True`. Its stream helper has no response-closing
`finally`; high-level `images.pull` drains the generator synchronously.
Consequently, a configured client timeout or a thread timeout is not a verified
pull deadline, and generator/client closure is not a documented guarantee of
disconnecting an active pull. A supervised CLI subprocess offers the documented
termination mechanism; reap it before returning a canceled result.
[SDK pull](https://github.com/docker/docker-py/blob/7.2.0/docker/api/image.py#L351-L435),
[SDK stream helper](https://github.com/docker/docker-py/blob/7.2.0/docker/api/client.py#L355-L372),
[SDK high-level pull](https://github.com/docker/docker-py/blob/7.2.0/docker/models/images.py#L415-L477)

The containerd backend creates a cancellable pull lease. On cancellation that
lease remains until its expiry, eight hours after creation, or pruning. Expiry
makes resources eligible for garbage collection; it does not promise immediate
reclamation. A failed or canceled pull therefore can retain partial content
without a completed image reference that ordinary image removal can reclaim.
[Pull lease creation](https://github.com/moby/moby/blob/docker-v29.9.0/daemon/containerd/image_pull.go#L55-L60),
[lease lifetime and cancellation](https://github.com/moby/moby/blob/docker-v29.9.0/daemon/containerd/leases.go#L10-L47)

## Size and disk guards

OCI descriptors give the byte size of their referenced blob. For compressed
layers this is the compressed payload, not an unpacked filesystem bound. A
manifest-size preflight can bound expected layer downloads but cannot establish
the pull's peak disk footprint.
[OCI descriptor specification](https://github.com/opencontainers/image-spec/blob/v1.1.1/descriptor.md#properties),
[OCI compressed-layer specification](https://github.com/opencontainers/image-spec/blob/v1.1.1/layer.md#gzip-media-types)

The containerd image store keeps compressed content and unpacked snapshots.
Its storage can be on a different filesystem from Docker's data root. Thus
monitoring only `DockerRootDir`, or only the client's local filesystem, cannot
cover every daemon configuration. Docker Desktop additionally stores Engine
data in a Linux VM volume with its own configured capacity.
[Engine storage documentation](https://docs.docker.com/engine/storage/containerd/#disk-space-usage),
[Desktop resource settings](https://docs.docker.com/desktop/settings-and-maintenance/settings/#advanced)

`docker system df` reports daemon usage and distinguishes shared from unique
image data. Summed image sizes overcount shared layers and do not predict bytes
reclaimed by removing an image.
[Docker disk accounting](https://docs.docker.com/reference/cli/docker/system/df/)

Implementation inference: serial pulls, a bounded owned cache, preflight
headroom, an absolute deadline, and concurrent free-space polling provide
useful stop guards. They cannot guarantee a hard disk ceiling: downloads and
extraction can grow between samples, another daemon client can consume space,
and canceled content can remain leased. Recheck storage after cancellation and
fail closed when capacity cannot be established or recovered. A strict physical
ceiling requires independently configured daemon/storage capacity, such as the
Desktop disk limit or a dedicated filesystem quota.

## Removal must preserve foreign references and containers

Ordinary image deletion supports references, IDs, `force`, and `noprune`.
Force can override stopped-container and tag conflicts; parent pruning is
enabled by default. Use `force=False`, `noprune=True`, and no platform-specific
deletion. Platform deletion can remove shared content across images and requires
force.
[Removal documentation](https://docs.docker.com/reference/cli/docker/image/rm/),
[SDK removal arguments](https://github.com/docker/docker-py/blob/7.2.0/docker/api/image.py#L503-L515)

**A digest reference is not a universal single-reference delete operation.** In
the modern containerd backend, `getSameReferences` selects all tags in the same
repository when the supplied reference contains a digest. When other references
remain, untagging occurs before container conflict checks. `force=False` alone
therefore does not guarantee preserving existing tags or references used by
containers.
[Containerd reference selection](https://github.com/moby/moby/blob/docker-v29.9.0/daemon/containerd/image_delete.go#L313-L377),
[untagging path and dependency checks](https://github.com/moby/moby/blob/docker-v29.9.0/daemon/containerd/image_delete.go#L76-L197)

Conservative implementation contract inferred from that behavior:

- Snapshot daemon identity, existing image IDs, tags, and digests before
  provisioning. Borrow existing images; never claim their cleanup ownership.
- Persist ownership only for exact immutable references acquired by this cache.
  If the post-pull image identity existed in the baseline, retain it as borrowed.
- Immediately before eviction, inspect again. Skip removal if any tag or digest
  is foreign, or any running or stopped container or image mount depends on the
  image. Remove only the owned immutable reference without force or parent prune.
- Reinspect after removal and measure actual capacity. An untag response is not
  proof that image bytes were reclaimed. On conflicts or unexpected state, keep
  the resource and report the capacity constraint; do not invoke global pruning.

The API does not provide an ownership-conditioned atomic delete: external
clients can change references between inspection and removal. Complete race
exclusion requires an exclusively managed daemon. This cache contract concerns
resource residency only; the frozen 150-task cohort remains intact, and a
capacity failure is a provisioning failure rather than permission to drop tasks.
