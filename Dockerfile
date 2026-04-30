FROM mambaorg/micromamba:2.5.0

USER root

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WORKDIR=/app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY --chown=$MAMBA_USER:$MAMBA_USER docker/runtime-env.yaml /tmp/runtime-env.yaml
RUN micromamba install -y -n base -f /tmp/runtime-env.yaml && \
    micromamba clean --all --yes

ARG MAMBA_DOCKERFILE_ACTIVATE=1

WORKDIR /app

COPY --chown=$MAMBA_USER:$MAMBA_USER pyproject.toml /app/pyproject.toml
COPY --chown=$MAMBA_USER:$MAMBA_USER src /app/src
COPY --chown=$MAMBA_USER:$MAMBA_USER api /app/api
COPY --chown=$MAMBA_USER:$MAMBA_USER tools /app/tools
COPY --chown=$MAMBA_USER:$MAMBA_USER hpo_extractor.py /app/hpo_extractor.py
COPY --chown=$MAMBA_USER:$MAMBA_USER config/clinical_mvp.template.json /app/config/clinical_mvp.template.json
COPY --chown=$MAMBA_USER:$MAMBA_USER docker/entrypoint.sh /app/docker/entrypoint.sh

RUN PYTHONNOUSERSITE=1 python -m pip install -e .

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/_entrypoint.sh", "/app/docker/entrypoint.sh"]
