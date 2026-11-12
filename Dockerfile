FROM python:3.11.14-slim
WORKDIR /workspace
COPY . .
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python3", "app.py", "demo"]
