FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /app

# Install Python dependencies first so Docker can cache this layer.
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r /app/requirements.txt

# Copy the experiment code. Dataset CSVs should be mounted at runtime,
# not baked into the image.
COPY . /app

# Catch syntax/import-bytecode issues during the image build.
RUN python -m compileall -q /app

# Default command only shows help. Override this with the fixed/sliding
# command when starting a container.
CMD ["python", "run_fixed_calibration.py", "--help"]
