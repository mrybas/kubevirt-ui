FROM mcr.microsoft.com/playwright:v1.58.2-noble

WORKDIR /e2e

# Install dependencies first for layer caching
COPY package.json package-lock.json* ./
RUN npm ci

# Copy test source
COPY . .

CMD ["npx", "playwright", "test"]
