FROM python:3.11-slim AS runtime

LABEL org.opencontainers.image.title="cashlyctl"
LABEL org.opencontainers.image.description="Terminal operations console and CLI for Cashly/DealSense deployments."

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV CASHLYCTL_HOME=/home/cashly/.cashlyctl

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 --shell /bin/sh cashly \
    && mkdir -p /app "${CASHLYCTL_HOME}" \
    && chown -R cashly:cashly /app /home/cashly

WORKDIR /app

COPY --chown=cashly:cashly pyproject.toml README.md ./
COPY --chown=cashly:cashly src ./src

RUN pip install .

USER cashly

VOLUME ["/home/cashly/.cashlyctl"]

ENTRYPOINT ["cashlyctl"]
CMD ["--help"]
