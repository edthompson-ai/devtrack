FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# Dependencies first so code changes don't bust the layer cache.
COPY pyproject.toml README.md ./
RUN mkdir -p src/devtrack && touch src/devtrack/__init__.py \
 && pip install ".[postgres]" && pip uninstall -y devtrack

COPY src ./src
COPY seed ./seed
COPY docs ./docs
RUN pip install --no-deps -e .

RUN useradd --create-home app && chown -R app /app
USER app
EXPOSE 8000
CMD ["devtrack", "demo", "--host", "0.0.0.0", "--port", "8000"]
