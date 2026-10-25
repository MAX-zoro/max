FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8765 DATA_DIR=/app/data
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock && useradd --create-home --uid 10001 collector && mkdir /app/data && chown collector:collector /app/data
COPY app.py monitor.py store.py ./
COPY static ./static
USER collector
EXPOSE 8765
CMD ["python", "app.py"]
