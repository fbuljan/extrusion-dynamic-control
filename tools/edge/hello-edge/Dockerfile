FROM python:3.11-slim
WORKDIR /app
COPY tools/edge/hello-edge/index.html .
CMD ["python", "-m", "http.server", "8000", "--bind", "0.0.0.0"]
