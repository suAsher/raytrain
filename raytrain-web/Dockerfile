# raytrain-web: build the SPA, serve with nginx (also proxies /v1 to server).
#
#   docker build -t 172.31.9.104:5050/raytrain/raytrain-web:v0.1 raytrain-web/
#   docker push 172.31.9.104:5050/raytrain/raytrain-web:v0.1
#
FROM node:20-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
# Reproducible install from the committed lockfile.
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.27-alpine AS runtime
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
