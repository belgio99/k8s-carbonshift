FROM python:3-alpine

WORKDIR /app

COPY requirements.txt .

RUN apk add --no-cache gcc g++ musl-dev python3-dev && \
   pip3 install -r requirements.txt && \
   apk del gcc g++ musl-dev python3-dev


COPY carbon-aware-service.py .
ADD flavours flavours

EXPOSE 50000
CMD python3 carbon-aware-service.py
