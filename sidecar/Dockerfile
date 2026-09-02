FROM python:3.11-slim AS builder
WORKDIR /sidecar
COPY requirements.txt .
RUN pip install --user --upgrade pip setuptools wheel
RUN pip install --user -r requirements.txt

FROM python:3.11-slim
WORKDIR /sidecar
RUN useradd -m sidecar-user
COPY --from=builder /root/.local /home/sidecar-user/.local
RUN chown -R sidecar-user:sidecar-user /home/sidecar-user/.local
COPY app.py .
ENV PATH=/home/sidecar-user/.local/bin:$PATH
USER sidecar-user
CMD ["python3", "app.py"]
