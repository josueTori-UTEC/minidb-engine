# Cliente web de MiniDB: compilación con Node y servido estático con nginx.

# --- Etapa 1: compilación (Vite 8 requiere Node ^20.19.0 || >=22.12.0) ---
FROM node:24-alpine AS build
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Vacío = el navegador llama a /api en el mismo origen y nginx lo reenvía al backend
ARG VITE_API_URL=""
ENV VITE_API_URL=$VITE_API_URL

RUN npm run build

# --- Etapa 2: servidor estático ---
FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
