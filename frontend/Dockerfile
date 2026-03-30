# syntax=docker/dockerfile:1

# =============================================================================
# Base stage
# =============================================================================
FROM node:22-alpine AS base

WORKDIR /app

# =============================================================================
# Development stage
# =============================================================================
FROM base AS development

COPY package.json package-lock.json* ./
RUN npm install --legacy-peer-deps && \
    npm install --legacy-peer-deps @rollup/rollup-linux-x64-musl

COPY . .

EXPOSE 3000

CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

# =============================================================================
# Build stage
# =============================================================================
FROM base AS builder

COPY package.json package-lock.json* ./
RUN npm ci --legacy-peer-deps

COPY . .
RUN npm run build

# =============================================================================
# Production stage
# =============================================================================
FROM nginx:alpine AS production

# Copy nginx config
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Copy built assets
COPY --from=builder /app/dist /usr/share/nginx/html

# Add non-root user
RUN adduser -D -H -u 1000 -s /sbin/nologin appuser && \
    chown -R appuser:appuser /usr/share/nginx/html && \
    chown -R appuser:appuser /var/cache/nginx && \
    chown -R appuser:appuser /var/log/nginx && \
    touch /var/run/nginx.pid && \
    chown -R appuser:appuser /var/run/nginx.pid

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:8080/health || exit 1

CMD ["nginx", "-g", "daemon off;"]
