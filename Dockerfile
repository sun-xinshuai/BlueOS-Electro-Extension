FROM python:3.9-slim-bullseye

# 不再需要 gcc，所有依赖都是纯 Python wheel
COPY app /app
RUN python /app/setup.py install

EXPOSE 80/tcp

LABEL version="1.0.0"
LABEL permissions='\
{\
  "ExposedPorts": {\
    "80/tcp": {}\
  },\
  "HostConfig": {\
    "Privileged": true,\
    "Binds": ["/dev:/dev"],\
    "PortBindings": {\
      "80/tcp": [\
        {\
          "HostPort": ""\
        }\
      ]\
    }\
  }\
}'
LABEL authors='[\
    {\
        "name": "Your Name",\
        "email": "you@example.com"\
    }\
]'
LABEL company='{\
    "about": "",\
    "name": "Your Company",\
    "email": "you@example.com"\
}'
LABEL type="tool"
LABEL tags='["serial", "uart", "monitor"]'
LABEL readme='https://raw.githubusercontent.com/your-repo/main/README.md'
LABEL links='{\
    "website": "https://github.com/your-repo",\
    "support": "https://github.com/your-repo/issues"\
}'
LABEL requirements="core >= 1.1"

ENTRYPOINT ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80", "--app-dir", "/app"]
