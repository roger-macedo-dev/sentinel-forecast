import os
from flask import Flask, request, jsonify
import psycopg2

app = Flask(__name__)

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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
