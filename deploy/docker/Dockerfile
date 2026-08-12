# Container image for the hub.
#
# Two stages: the first builds a virtual environment with uv, the second copies
# only that environment into a clean base. The final image therefore has no
# compiler, no headers and no build cache, which is both smaller and a smaller
# attack surface.
#
# The vahub package is installed, not bind-mounted. An image that only works
# when the source tree happens to be mounted next to it is not a deployment
# artifact. The developer overlay (docker-compose.dev.yml) puts the source back
# on PYTHONPATH when that is what you want.
#
# Build from the repository root:
#   docker build -f deploy/docker/Dockerfile -t vahub:local .

# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------
FROM python:3.12-slim AS build

# uv is pinned: an installer that changes under you defeats the point of
# building an image in the first place.
COPY --from=ghcr.io/astral-sh/uv:0.11.31 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1

WORKDIR /src

# pyproject.toml declares readme = "README.md" and a license file, so hatchling
# needs both present before it can build the wheel.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN uv venv /opt/vahub/venv --python /usr/local/bin/python3.12 \
 && uv pip install --python /opt/vahub/venv/bin/python --no-cache /src

# --------------------------------------------------------------------------
# runtime
# --------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ARG VERSION=0.0.0
ARG REVISION=unknown

LABEL org.opencontainers.image.title="vahub" \
      org.opencontainers.image.description="Voice assistant hub with a policy gate on every action" \
      org.opencontainers.image.source="https://github.com/LynnDelpy/vahub" \
      org.opencontainers.image.documentation="https://github.com/LynnDelpy/vahub/tree/main/docs" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.base.name="docker.io/library/python:3.12-slim"

# git is here for `vahub module add`, which installs a module from a pinned git
# revision at runtime. It is a fetcher, not a build tool: there is deliberately
# no compiler in this image, so a module whose dependencies have no wheels will
# fail to install. That is the intended trade, and it is a reason to prefer
# modules that publish wheels.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

# uv again, for building the per-module virtual environments under the state
# directory. Same pin as the build stage.
COPY --from=ghcr.io/astral-sh/uv:0.11.31 /uv /usr/local/bin/uv

COPY --from=build /opt/vahub/venv /opt/vahub/venv

# HOME points at the state volume so nothing needs to write outside it. That is
# what makes `read_only: true` on the container workable.
ENV PATH="/opt/vahub/venv/bin:${PATH}" \
    HOME=/var/lib/vahub \
    XDG_CACHE_HOME=/var/lib/vahub/.cache \
    UV_CACHE_DIR=/var/lib/vahub/.cache/uv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    VAHUB_CONFIG=/etc/vahub/vahub.yaml \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# A fixed uid, so the named volume keeps working when the image is rebuilt and
# so a host bind mount can be chowned to a known owner.
RUN useradd --uid 10001 --user-group --no-create-home \
        --home-dir /var/lib/vahub --shell /usr/sbin/nologin vahub \
 && mkdir -p /var/lib/vahub /etc/vahub/modules.d \
 && chown -R 10001:10001 /var/lib/vahub /etc/vahub

WORKDIR /var/lib/vahub
USER 10001:10001

EXPOSE 8080

# The port is fixed here because a HEALTHCHECK cannot see the parsed config. If
# you move web.port, override the healthcheck in compose as well.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/health', timeout=4)"

# Entrypoint is the CLI, so `docker run vahub:local doctor` and
# `docker run vahub:local module list` work without remembering a path.
ENTRYPOINT ["vahub"]
CMD ["run"]
