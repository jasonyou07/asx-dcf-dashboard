# ASX DCF Dashboard — Streamlit app, containerized.
FROM python:3.12-slim

WORKDIR /app

# System deps: curl is needed only for the HEALTHCHECK below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first so this layer is cached across code-only changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code only — tests, dev tooling, and local venv are excluded via .dockerignore.
COPY dcf_dashboard.py dcf_engine.py excel_export.py ./

# Run as a non-root user.
RUN useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl --fail http://localhost:8501/_stcore/health || exit 1

ENTRYPOINT ["streamlit", "run", "dcf_dashboard.py", \
    "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]
