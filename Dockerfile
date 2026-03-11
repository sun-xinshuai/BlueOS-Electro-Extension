FROM python:3.11-slim

LABEL version="1.0.0"
LABEL permissions='\
{\
  "ExposedPorts": {\
    "5000/tcp": {}\
  },\
  "HostConfig": {\
    "Binds": ["/dev:/dev"],\
    "Devices": [{\
      "PathOnHost": "/dev/ttyAMA0",\
      "PathInContainer": "/dev/ttyAMA0",\
      "CgroupPermissions": "rwm"\
    }],\
    "PortBindings": {\
      "5000/tcp": [{"HostPort": "5000"}]\
    },\
    "Privileged": true\
  }\
}'
LABEL authors='[\
  {\
    "name": "Serial Monitor Extension",\
    "email": "dev@blueos.local"\
  }\
]'
LABEL company='{"about":"","name":"BlueOS Extensions","email":""}'
LABEL type="tool"
LABEL readme='https://raw.githubusercontent.com/your-repo/main/README.md'
LABEL links='{"website":"https://blueos.local"}'
LABEL requirements=""

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ make libssl-dev \
    && rm -rf /var/lib/apt/lists/*
# Install dependencies
COPY serial/backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY serial/backend/ ./backend/
COPY serial/frontend/ ./frontend/

# Patch main.py to serve from correct path
RUN sed -i 's|/app/frontend|/app/frontend|g' backend/main.py

EXPOSE 5000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "5000", "--log-level", "info"]
