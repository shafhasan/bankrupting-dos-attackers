FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY trace_weibull_experiment.py ./trace_weibull_experiment.py

ENTRYPOINT ["python", "/app/trace_weibull_experiment.py"]
