# Build the V2 dashboard
FROM node:22-bookworm-slim AS dashboard-build

WORKDIR /src/dashboard-v2

COPY dashboard-v2/package*.json ./
RUN npm ci

COPY dashboard-v2/ ./
RUN npm run build

# Python runtime
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/config

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Include the built dashboard
COPY --from=dashboard-build /src/dashboard-v2/dist ./dashboard-v2/dist

EXPOSE 7777

CMD ["sh", "start_scanner.sh", "--host", "0.0.0.0", "--port", "7777"]
