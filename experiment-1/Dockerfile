FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /app/requirements.txt

COPY experiment-1.py /app/experiment-1.py

RUN mkdir -p /results

ENTRYPOINT ["python", "/app/experiment-1.py"]
CMD ["--output-dir", "/results/default"]
