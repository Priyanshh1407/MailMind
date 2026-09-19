FROM python:3.14-slim AS backend
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY requirements.lock ./
RUN pip install --no-cache-dir torch==2.14.0 --index-url https://download.pytorch.org/whl/cpu && pip install --no-cache-dir -r requirements.lock && pip check
RUN groupadd --gid 10001 mailmind && useradd --uid 10001 --gid 10001 --create-home mailmind && mkdir -p /app/data /app/models && chown -R mailmind:mailmind /app
COPY src/ ./src/
COPY api/ ./api/
COPY scripts/ ./scripts/
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=90s --retries=6 CMD ["python", "-m", "scripts.wait_ready", "--timeout", "2"]
CMD ["python", "-m", "uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000"]

FROM node:22.17.1-slim AS ui-build
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM node:22.17.1-slim AS frontend
ENV NODE_ENV=production MAILMIND_UI_HOST=0.0.0.0 MAILMIND_STDIN_CONTROL=false
WORKDIR /ui
COPY --from=ui-build --chown=node:node /ui/dist ./dist
COPY --chown=node:node frontend/serve.mjs ./serve.mjs
USER node
EXPOSE 5173
HEALTHCHECK --interval=10s --timeout=3s --retries=6 CMD ["node", "-e", "fetch('http://127.0.0.1:5173/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
CMD ["node", "serve.mjs"]
