FROM python:3.12-slim
WORKDIR /app
COPY . .
# Oeffentliche Version: eigene Texturen, Einladungscode (HUB_INVITE im Hoster eintragen), Daten auf dem dauerhaften Speicher /data
ENV HUB_PUBLIC=1 HUB_HTTPS=1 HUB_DATA=/data PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "server.py"]
