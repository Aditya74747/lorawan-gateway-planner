# Serves the 3D planner + API. Build after `uv run hnplan all` so web/data and the small
# processed files exist (raw geodata is NOT needed in the image).
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md config.yaml ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY web ./web
COPY data/processed/cells.parquet data/processed/sites.parquet data/processed/predictions.parquet ./data/processed/
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "hnplan", "serve", "--port", "8000"]
