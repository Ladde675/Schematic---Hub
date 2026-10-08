FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN mkdir -p static downloads && (mv -n three.min.js static/ || true) && (mv -n blueprint-1.0.0.jar downloads/ || true)
ENV HUB_PUBLIC=1 HUB_HTTPS=1 HUB_DATA=/data PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "server.py"]
