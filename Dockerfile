# 随行地图 MapGo — 零第三方依赖,无需 npm install
FROM node:22-alpine

WORKDIR /app
COPY package.json server.js ./
COPY src ./src
COPY public ./public

ENV NODE_ENV=production \
    PORT=3000 \
    DATA_DIR=/app/data

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD wget -qO- http://127.0.0.1:3000/api/health || exit 1

CMD ["node", "server.js"]
