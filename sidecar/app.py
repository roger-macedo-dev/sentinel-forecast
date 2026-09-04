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
INTERVALO_SEGUNDOS = int(os.environ.get("INTERVALO_SEGUNDOS", "300"))
HORIZONTE_HORAS = int(os.environ.get("HORIZONTE_HORAS", "1"))
# Janela de historico usada para treinar. Uma hora so permite ao modelo
# capturar tendencia; com dias, ele passa a reconhecer sazonalidade diaria
# (madrugada vs horario comercial) e a anomalia deixa de ser "fora da media
# recente" e vira "fora do padrao daquele horario".
JANELA_SEGUNDOS = int(os.environ.get("JANELA_SEGUNDOS", "3600"))
# Limite pratico do query_range do Prometheus; manter folga.
MAXIMO_PONTOS = int(os.environ.get("MAXIMO_PONTOS", "3000"))

# Uma unica metrica com rotulo 'alvo', em vez de uma metrica por recurso:
# permite agregar e escrever uma regra de alerta que cobre todos os alvos.
previsao = Gauge(
    "previsao_percentual",
    "Previsao de uso do recurso no horizonte configurado, em percentual",
    ["alvo"],
)
previsao_segundos = Gauge(
    "previsao_segundos",
    "Previsao de duracao no horizonte configurado, em segundos",
    ["alvo"],
)
anomalia = Gauge(
    "anomalia_detectada",
    "1 quando o valor atual esta fora do intervalo esperado pelo modelo",
    ["alvo"],
)
valor_observado = Gauge(
    "valor_observado",
    "Ultimo valor real da metrica monitorada",
    ["alvo"],
)
limite_inferior = Gauge(
    "limite_inferior_esperado",
    "Piso do intervalo de confianca do modelo para o momento atual",
    ["alvo"],
)
limite_superior = Gauge(
    "limite_superior_esperado",
    "Teto do intervalo de confianca do modelo para o momento atual",
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


def buscar_historico(query, janela_segundos=None):
    """Busca a serie historica no Prometheus e devolve no formato que o Prophet
    espera: colunas 'ds' (timestamp) e 'y' (valor)."""
    janela_segundos = janela_segundos or JANELA_SEGUNDOS

    # O passo acompanha a janela: passo fixo de 30s numa janela de dias
    # ultrapassaria o teto de pontos do query_range e a consulta falharia.
    passo = max(30, int(janela_segundos / MAXIMO_PONTOS))
    agora = time.time()
    resp = requests.get(
        f"{PROMETHEUS_URL}/api/v1/query_range",
        params={
            "query": query,
            "start": agora - janela_segundos,
            "end": agora,
            "step": f"{passo}s",
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
    # NaN aparece quando a query nao tem dado no instante (ex: histogram_quantile
    # sem trafego). Manter NaN e perigoso: comparacao com NaN e sempre falsa,
    # entao a anomalia ficaria zerada sem ninguem perceber.
    df = df.dropna()
    return df


MINIMO_PONTOS = int(os.environ.get("MINIMO_PONTOS", "20"))


def prever(alvo):
    """Treina um modelo com o historico recente e devolve o valor previsto para
    o fim do horizonte configurado."""
    df = buscar_historico(alvo["query"], alvo.get("janela_segundos"))
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

    # changepoint_prior_scale menor = tendencia menos flexivel. O padrao (0.05)
    # persegue picos recentes e, com historico curto, extrapola demais.
    modelo = Prophet(
        growth="logistic",
        changepoint_prior_scale=float(alvo.get("flexibilidade", 0.01)),
        # Intervalo mais largo que o padrao (0.80). Metrica estavel tem variancia
        # minima, e um intervalo apertado transforma ruido normal em anomalia.
        interval_width=float(alvo.get("confianca", 0.99)),
    )
    modelo.fit(df)

    futuro = modelo.make_future_dataframe(periods=HORIZONTE_HORAS, freq="1h")
    futuro["cap"] = teto
    futuro["floor"] = 0.0

    resultado = modelo.predict(futuro)
    previsto = float(resultado.iloc[-1]["yhat"])

    # Rede de seguranca: o intervalo de confianca do modelo ainda pode escapar
    # um pouco dos limites mesmo com crescimento logistico.
    previsto = min(max(previsto, 0.0), teto)

    # Indexa pelo tamanho do historico, nao por posicao relativa ao fim: o
    # dataframe de previsao tem len(df) linhas historicas seguidas de
    # HORIZONTE_HORAS linhas futuras. Usar iloc[-2] so funcionaria com horizonte
    # de exatamente 1 hora. Comparar o valor real com o intervalo previsto
    # para esse instante e o que caracteriza anomalia: nao um limiar fixo, e sim
    # desvio em relacao ao que o modelo esperava dado o padrao historico.
    esperado_agora = resultado.iloc[len(df) - 1]
    multiplicador = alvo.get("multiplicador", 1)

    # Margem absoluta minima, proporcional ao teto da metrica. Sem ela, uma serie
    # praticamente constante gera faixa quase nula e acusa anomalia a cada
    # variacao irrelevante.
    margem = float(alvo.get("margem", 0.02)) * teto
    inferior_bruto = float(esperado_agora["yhat_lower"]) - margem
    superior_bruto = float(esperado_agora["yhat_upper"]) + margem

    return {
        "previsto": previsto * multiplicador,
        "observado": float(df["y"].iloc[-1]) * multiplicador,
        "inferior": min(max(inferior_bruto, 0.0), teto) * multiplicador,
        "superior": min(max(superior_bruto, 0.0), teto) * multiplicador,
    }


def ciclo():
    alvos = carregar_config()
    log.info("Previsoes configuradas: %s", [a["nome"] for a in alvos])

    while True:
        for alvo in alvos:
            nome = alvo["nome"]
            try:
                resultado = prever(alvo)
            except Exception as erro:
                # Falha em um alvo nao pode derrubar a previsao dos demais.
                falhas.labels(alvo=nome).set(1)
                log.error("Falha ao prever '%s': %s", nome, erro)
                continue

            falhas.labels(alvo=nome).set(0)
            if resultado is None:
                log.info("Sem historico suficiente para prever '%s'", nome)
                continue

            valor = resultado["previsto"]
            if alvo.get("unidade", "percentual") == "segundos":
                previsao_segundos.labels(alvo=nome).set(valor)
                log.info("Previsao de '%s': %.3fs", nome, valor)
            else:
                previsao.labels(alvo=nome).set(valor)
                log.info("Previsao de '%s': %.2f%%", nome, valor)

            observado = resultado["observado"]
            inferior = resultado["inferior"]
            superior = resultado["superior"]

            valor_observado.labels(alvo=nome).set(observado)
            limite_inferior.labels(alvo=nome).set(inferior)
            limite_superior.labels(alvo=nome).set(superior)

            fora_do_esperado = observado < inferior or observado > superior
            anomalia.labels(alvo=nome).set(1 if fora_do_esperado else 0)

            if fora_do_esperado:
                log.warning(
                    "Anomalia em '%s': valor %.4f fora do intervalo esperado [%.4f, %.4f]",
                    nome, observado, inferior, superior,
                )

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
