# The board (ADR 0018): built with Node, served by nginx. The Node toolchain stays in the build
# stage; the runtime image is nginx and static files.
FROM node:22-alpine AS build

WORKDIR /board

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.30-alpine

COPY nginx.conf /etc/nginx/nginx.conf
COPY --from=build /board/dist /usr/share/nginx/html

EXPOSE 8080
