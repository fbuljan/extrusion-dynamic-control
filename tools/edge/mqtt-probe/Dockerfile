FROM python:3.12-slim
WORKDIR /app
COPY tools/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY tools/mqtt/common.py tools/edge/mqtt-probe/probe_service.py ./
CMD ["python", "-u", "probe_service.py"]
