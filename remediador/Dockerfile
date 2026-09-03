FROM python:3.11-slim AS builder
WORKDIR /remediador
COPY requirements.txt .
RUN pip install --user -r requirements.txt

FROM python:3.11-slim
WORKDIR /remediador
RUN rm -rf /usr/local/lib/python3.11/site-packages/pip* /usr/local/lib/python3.11/site-packages/setuptools* /usr/local/lib/python3.11/site-packages/pkg_resources /usr/local/lib/python3.11/site-packages/wheel* /usr/local/lib/python3.11/ensurepip /usr/local/bin/pip*
RUN useradd -m remediador-user
COPY --from=builder /root/.local /home/remediador-user/.local
RUN chown -R remediador-user:remediador-user /home/remediador-user/.local
COPY app.py .
ENV PATH=/home/remediador-user/.local/bin:$PATH
USER remediador-user
CMD ["python3", "-u", "app.py"]
