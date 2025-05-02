FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY decision-engine.py .

EXPOSE 5001

CMD ["python", "decision-engine.py"]