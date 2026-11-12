FROM python:3.11.2-slim-bullseye@sha256:9ad4ffc502779e5508f7ac1eccab4a22786b80bd53d721d735f6de0840b245a1
WORKDIR /workspace
COPY requirements-build.txt .
RUN python3 -m pip install --no-cache-dir -r requirements-build.txt
COPY . .
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python3", "app.py", "demo"]
