FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./

# The versions uv.lock pins, not the newest ones: an unpinned install picks
# up whatever was released last, and a new release of a dependency can
# break fcli on import.
RUN uv export --frozen --no-dev --all-extras --no-emit-project --no-hashes -o /tmp/requirements.txt && \
    uv pip install --system --no-cache -r /tmp/requirements.txt && \
    rm /tmp/requirements.txt

COPY src ./src
# No .git in the image: hatch-vcs takes the version from here instead.
ARG VERSION=0.0.0
RUN SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION} uv pip install --system --no-cache --no-deps . && \
    rm -rf /app

# The history and baselines go under one fixed directory, whatever user the
# container runs as, so a volume mounted there keeps them. World-writable and
# sticky like /tmp, so a container run with --user can write to it too.
ENV XDG_STATE_HOME=/state
RUN mkdir -p /state && chmod 1777 /state
VOLUME /state

WORKDIR /work
EXPOSE 8765
ENTRYPOINT ["fcling"]
CMD ["--help"]
