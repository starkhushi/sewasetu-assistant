FROM python:3.12-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sewasetu_mcp ./sewasetu_mcp

# Which server to run is chosen by the compose service: sewasetu_mcp.citizen or .officer
CMD ["python", "-m", "sewasetu_mcp.citizen"]
