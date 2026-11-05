FROM ubuntu:24.04
RUN apt-get update \
 && apt-get install -y --no-install-recommends qemu-system-x86 qemu-utils python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY app.py index.html /app/
EXPOSE 8080
CMD ["python3", "/app/app.py"]
