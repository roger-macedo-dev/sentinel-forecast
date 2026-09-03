import os
import time
import logging
import threading

import yaml
import requests
import pandas as pd
from prophet import Prophet
from flask import Flask
from prometheus_client import Gauge, generate_latest, CONTENT_TYPE_LATEST

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# O Prophet e o cmdstanpy sao muito verbosos a cada treino; silencia o ruido.
logging.getLogger("cmdstanpy").setLevel(logging.WARNING)
logging.getLogger("prophet").setLevel(logging.WARNING)
log = logging.getLogger("sidecar")

app = Flask(__name__)

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
CONFIG_PATH = os.environ.get("PREVISOES_CONFIG", "/config/previsoes.yml")
INTERVALO_SEGUNDOS = int(os.environ.get("INTERVALO_SEGUNDOS", "60"))
HORIZONTE_HORAS = int(os.environ.get("HORIZONTE_HORAS", "1"))

# Uma unica metrica com rotulo 'alvo', em vez de uma metrica por recurso:
# permite agregar e escrever uma regra de alerta que cobre todos os alvos.
previsao = Gauge(
    "previsao_percentual",
    "Previsao de uso do recurso no horizonte configurado, em percentual",
    ["alvo"],
)
falhas = Gauge(
    "previsao_falhas",
    "1 se a ultima tentativa de previsao do alvo falhou, 0 se teve sucesso",
    ["alvo"],
)


def carregar_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)["previsoes"]


def buscar_historico(query, janela_segundos=3600, passo="30s"):
    """Busca a serie historica no Prometheus e devolve no formato que o Prophet
    espera: colunas 'ds' (timestamp) e 'y' (valor)."""
    agora = time.time()
    resp = requests.get(
        f"{PROMETHEUS_URL}/api/v1/query_range",
        params={
            "query": query,
            "start": agora - janela_segundos,
            "end": agora,
            "step": passo,
        },
        timeout=30,
    )
    resp.raise_for_status()
    dados = resp.json()["data"]["result"]
    if not dados:
        return None

    valores = dados[0]["values"]
    df = pd.DataFrame(valores, columns=["ds", "y"])
    df["ds"] = pd.to_datetime(df["ds"], unit="s")
    df["y"] = df["y"].astype(float)
    return df


MINIMO_PONTOS = int(os.environ.get("MINIMO_PONTOS", "20"))


def prever(alvo):
    """Treina um modelo com o historico recente e devolve o valor previsto para
    o fim do horizonte configurado."""
    df = buscar_historico(alvo["query"])
    if df is None or len(df) < MINIMO_PONTOS:
        # Historico curto gera tendencia instavel: uma queda momentanea vira
        # extrapolacao absurda. Comum logo apos subir o ambiente.
        return None

    # A metrica e limitada (ex: uso de recurso vai de 0 ate o teto). Sem informar
    # isso, o Prophet usa crescimento linear ilimitado e extrapola a tendencia
    # recente indefinidamente, produzindo previsoes fisicamente impossiveis
    # (uso negativo, ou centenas de por cento).
    teto = float(alvo.get("teto", 1.0))
    df["cap"] = teto
    df["floor"] = 0.0

    modelo = Prophet(growth="logistic")
    modelo.fit(df)

    futuro = modelo.make_future_dataframe(periods=HORIZONTE_HORAS, freq="1h")
    futuro["cap"] = teto
    futuro["floor"] = 0.0

    resultado = modelo.predict(futuro)
    previsto = float(resultado.iloc[-1]["yhat"])

    # Rede de seguranca: o intervalo de confianca do modelo ainda pode escapar
    # um pouco dos limites mesmo com crescimento logistico.
    previsto = min(max(previsto, 0.0), teto)
    return previsto * alvo.get("multiplicador", 1)


def ciclo():
    alvos = carregar_config()
    log.info("Previsoes configuradas: %s", [a["nome"] for a in alvos])

    while True:
        for alvo in alvos:
            nome = alvo["nome"]
            try:
                valor = prever(alvo)
            except Exception as erro:
                # Falha em um alvo nao pode derrubar a previsao dos demais.
                falhas.labels(alvo=nome).set(1)
                log.error("Falha ao prever '%s': %s", nome, erro)
                continue

            falhas.labels(alvo=nome).set(0)
            if valor is None:
                log.info("Sem historico suficiente para prever '%s'", nome)
                continue

            previsao.labels(alvo=nome).set(valor)
            log.info("Previsao de '%s': %.2f%%", nome, valor)

        time.sleep(INTERVALO_SEGUNDOS)


@app.route("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


@app.route("/health")
def health():
    return {"status": "ok", "config": CONFIG_PATH, "horizonte_horas": HORIZONTE_HORAS}


if __name__ == "__main__":
    threading.Thread(target=ciclo, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)
