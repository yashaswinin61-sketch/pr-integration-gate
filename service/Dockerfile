FROM python:3.12-alpine
RUN adduser -D -u 10001 svc
COPY app.py /app/app.py
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=2s --timeout=2s --start-period=5s --retries=10 \
  CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1
ENTRYPOINT ["python", "-u", "/app/app.py"]
