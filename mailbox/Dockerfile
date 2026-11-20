# relay mailbox: standard library only, runs as an unprivileged user.
# Pin the base image by digest when deploying (e.g. python:3.12-slim@sha256:...).
FROM python:3.12-slim

RUN useradd --system --uid 10001 --no-create-home relay \
 && mkdir /data && chown relay /data
COPY relay_mailbox /app/relay_mailbox
WORKDIR /app
USER relay
ENV PYTHONUNBUFFERED=1 RELAY_MAILBOX_DATA=/data RELAY_MAILBOX_HOST=127.0.0.1 RELAY_MAILBOX_PORT=8484
VOLUME /data
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8484/healthz', timeout=3).status == 200 else 1)"
ENTRYPOINT ["python", "-m", "relay_mailbox.server"]
CMD ["serve"]
