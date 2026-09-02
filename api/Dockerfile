FROM python:3.11-slim AS builder
WORKDIR /api
COPY requirements.txt .
RUN pip install --user --upgrade pip setuptools wheel
RUN pip install --user -r requirements.txt

FROM python:3.11-slim
WORKDIR /api
RUN useradd -m api-user
COPY --from=builder /root/.local /home/api-user/.local
RUN chown -R api-user:api-user /home/api-user/.local
COPY app.py .
ENV PATH=/home/api-user/.local/bin:$PATH
USER api-user
CMD ["python3", "app.py"]
