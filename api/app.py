import os
import time

from flask import Flask, request, jsonify, g, Response
import psycopg2
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

app = Flask(__name__)

# --- Instrumentacao -----------------------------------------------------------
# A rota usada como rotulo e o TEMPLATE da rota ("/envios/<cliente>"), nao o
# caminho concreto. Usar o caminho geraria uma serie nova por cliente, e a
# cardinalidade da metrica cresceria sem limite.
requisicoes = Counter(
    "http_requisicoes_total",
    "Total de requisicoes HTTP",
    ["metodo", "rota", "status"],
)
duracao = Histogram(
    "http_duracao_segundos",
    "Duracao das requisicoes HTTP",
    ["metodo", "rota"],
)


@app.before_request
def marcar_inicio():
    g.inicio = time.perf_counter()


@app.after_request
def registrar_metricas(resposta):
    # O proprio endpoint de metricas nao e instrumentado: o scrape do Prometheus
    # inflaria a contagem de requisicoes da aplicacao.
    if request.path == "/metrics":
        return resposta

    rota = request.url_rule.rule if request.url_rule else "desconhecida"
    requisicoes.labels(
        metodo=request.method, rota=rota, status=resposta.status_code
    ).inc()
    duracao.labels(metodo=request.method, rota=rota).observe(
        time.perf_counter() - g.inicio
    )
    return resposta


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "postgres"),
        dbname=os.environ.get("DB_NAME", "sentinel_forecast"),
        user=os.environ.get("DB_USER", "sentinel"),
        password=os.environ.get("DB_PASSWORD", "sentinel123"),
    )


CLIENTES_VALIDOS = {"norte", "sul", "leste"}


@app.route("/envios/<cliente>", methods=["POST"])
def criar_envio(cliente):
    if cliente not in CLIENTES_VALIDOS:
        return jsonify({"erro": "cliente invalido"}), 400
    dados = request.get_json()
    destino = dados.get("destino", "")
    schema = f"cliente_{cliente}"

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"INSERT INTO {schema}.envios (destino) VALUES (%s) RETURNING id", (destino,))
    novo_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"id": novo_id, "status": "criado"}), 201


@app.route("/envios/<cliente>", methods=["GET"])
def listar_envios(cliente):
    if cliente not in CLIENTES_VALIDOS:
        return jsonify({"erro": "cliente invalido"}), 400
    schema = f"cliente_{cliente}"

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"SELECT id, destino, status, criado_em FROM {schema}.envios ORDER BY id DESC LIMIT 20")
    linhas = cur.fetchall()
    cur.close()
    conn.close()

    return jsonify([{"id": r[0], "destino": r[1], "status": r[2], "criado_em": str(r[3])} for r in linhas])


@app.route("/health")
def health():
    return {"status": "ok"}


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
