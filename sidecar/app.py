import os                                    # variaveis de ambiente
import time                                  # espera entre ciclos de treino
import threading                             # roda o treino em paralelo, sem travar o /metrics
import requests                              # consulta a API do Prometheus
import pandas as pd                          # formato de dado exigido pelo Prophet
from prophet import Prophet                  # biblioteca de forecasting
from flask import Flask, Response            # expoe o endpoint /metrics

app = Flask(__name__)
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
QUERY = '1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)'  # % de memoria usada

ultima_previsao = None                       # guarda o resultado mais recente, lido pelo /metrics

def buscar_historico():
    agora = time.time()
    inicio = agora - 3600                    # ultima 1h de historico
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query_range", params={
        "query": QUERY,
        "start": inicio,
        "end": agora,
        "step": "30s",
    })
    dados = resp.json()["data"]["result"]
    if not dados:
        return None
    valores = dados[0]["values"]             # lista de [timestamp, valor]
    df = pd.DataFrame(valores, columns=["ds", "y"])
    df["ds"] = pd.to_datetime(df["ds"], unit="s")   # Prophet exige datetime
    df["y"] = df["y"].astype(float)
    return df

def treinar_e_prever():
    global ultima_previsao
    while True:
        df = buscar_historico()
        if df is not None and len(df) >= 10:  # Prophet precisa de um minimo de pontos
            modelo = Prophet()
            modelo.fit(df)
            futuro = modelo.make_future_dataframe(periods=1, freq="1h")  # projeta 1h a frente
            previsao = modelo.predict(futuro)
            ultima_previsao = previsao.iloc[-1]["yhat"] * 100   # valor previsto, em %
            print(f"Nova previsao: {ultima_previsao:.2f}%")
        time.sleep(60)                        # retreina a cada 1 minuto

@app.route("/metrics")
def metrics():
    if ultima_previsao is None:
        return Response("# sem previsao ainda\n", mimetype="text/plain")
    linha = f"memoria_previsao_percentual {ultima_previsao:.4f}\n"
    return Response(linha, mimetype="text/plain")

if __name__ == "__main__":
    threading.Thread(target=treinar_e_prever, daemon=True).start()  # treino roda em background
    app.run(host="0.0.0.0", port=8000)
