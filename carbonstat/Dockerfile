FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY carbon-aware-service.py .
ADD flavours flavours

EXPOSE 50000
CMD ["python", "carbon-aware-service.py"]
