FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

WORKDIR /app

# Install the system CA tools
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Add the organization or private CA certificate
COPY certs/ca-bundle.crt /usr/local/share/ca-certificates/navtracker-ca.crt

# Rebuild the Debian system trust store
RUN update-ca-certificates

RUN addgroup --system appgroup \
    && adduser --system --ingroup appgroup appuser

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config ./config

RUN chown -R appuser:appgroup /app

USER appuser

CMD ["python", "-m", "app.main"]
