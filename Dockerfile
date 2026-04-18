FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bridge script
COPY slack-bridge.py .

# Health check: verify the process is running
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD pgrep -f slack-bridge.py || exit 1

# Run the bridge
CMD ["python", "-u", "slack-bridge.py"]
