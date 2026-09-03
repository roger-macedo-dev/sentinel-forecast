import os
import time
import logging

from flask import Flask, request, jsonify
from prometheus_client import Counter, generate_latest, CONTENT_TYPE_LATEST

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("remediador")

app = Flask(__name__)

# --- Configuracao por ambiente -------------------------------------------------
# DRY_RUN=true  -> so registra o que faria (usado no Compose local, onde nao ha cluster)
# DRY_RUN=false -> executa de verdade contra a API do Kubernetes
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

# Allowlist: unicos alvos que o remediador tem permissao de reiniciar.
# Alvo que nao estiver aqui e recusado, mesmo que o alerta peca.
ALLOWLIST = {
    alvo.strip()
    for alvo in os.environ.get("ALLOWLIST", "sidecar,api").split(",")
    if alvo.strip()
}

# Cooldown: tempo minimo entre duas remediacoes do mesmo alvo.
# Sem isso, um alerta que persiste apos o restart viraria loop de restart.
COOLDOWN_SEGUNDOS = int(os.environ.get("COOLDOWN_SEGUNDOS", "600"))

NAMESPACE = os.environ.get("NAMESPACE", "default")

# --- Metricas -----------------------------------------------------------------
remediacoes = Counter(
    "remediacoes_total",
    "Remediacoes executadas pelo remediador",
    ["alvo", "resultado"],
)
bloqueios = Counter(
    "remediacoes_bloqueadas_total",
    "Remediacoes recusadas pelas protecoes",
    ["motivo"],
)

# Ultima remediacao por alvo, para o controle de cooldown.
ultima_execucao = {}


def reiniciar_deployment(alvo):
    """Equivalente ao `kubectl rollout restart deployment/<alvo>`: grava uma
    annotation com o horario atual no template do Pod, o que muda o template e
    faz o Kubernetes conduzir um rolling update."""
    from kubernetes import client, config

    config.load_incluster_config()
    apps = client.AppsV1Api()

    corpo = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "remediador/reiniciado-em": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    }
                }
            }
        }
    }
    apps.patch_namespaced_deployment(name=alvo, namespace=NAMESPACE, body=corpo)


def processar(alerta):
    labels = alerta.get("labels", {})
    alvo = labels.get("target")
    severidade = labels.get("severity")

    # So age em alerta disparando; alerta resolvido nao gera acao.
    if alerta.get("status") != "firing":
        bloqueios.labels(motivo="nao_esta_disparando").inc()
        return "ignorado: alerta nao esta disparando"

    # Defesa em profundidade: o roteamento do Alertmanager ja manda so critical
    # para ca, mas o servico nao confia nisso e valida de novo.
    if severidade != "critical":
        bloqueios.labels(motivo="severidade_insuficiente").inc()
        return f"ignorado: severidade {severidade}"

    if not alvo:
        bloqueios.labels(motivo="sem_alvo").inc()
        return "recusado: alerta sem rotulo 'target'"

    if alvo not in ALLOWLIST:
        bloqueios.labels(motivo="fora_da_allowlist").inc()
        log.warning("Alvo '%s' recusado: fora da allowlist %s", alvo, sorted(ALLOWLIST))
        return f"recusado: '{alvo}' fora da allowlist"

    agora = time.time()
    anterior = ultima_execucao.get(alvo)
    if anterior and (agora - anterior) < COOLDOWN_SEGUNDOS:
        restante = int(COOLDOWN_SEGUNDOS - (agora - anterior))
        bloqueios.labels(motivo="cooldown").inc()
        log.info("Alvo '%s' em cooldown, faltam %ss", alvo, restante)
        return f"adiado: cooldown ativo, faltam {restante}s"

    if DRY_RUN:
        ultima_execucao[alvo] = agora
        remediacoes.labels(alvo=alvo, resultado="dry_run").inc()
        log.info("[DRY-RUN] reiniciaria o deployment '%s'", alvo)
        return f"dry-run: reiniciaria '{alvo}'"

    try:
        reiniciar_deployment(alvo)
    except Exception as erro:
        remediacoes.labels(alvo=alvo, resultado="erro").inc()
        log.error("Falha ao reiniciar '%s': %s", alvo, erro)
        return f"erro ao reiniciar '{alvo}': {erro}"

    ultima_execucao[alvo] = agora
    remediacoes.labels(alvo=alvo, resultado="sucesso").inc()
    log.info("Deployment '%s' reiniciado por remediacao automatica", alvo)
    return f"reiniciado: '{alvo}'"


@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_json(silent=True) or {}
    resultados = [processar(alerta) for alerta in payload.get("alerts", [])]
    return jsonify({"resultados": resultados}), 200


@app.route("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


@app.route("/health")
def health():
    return {
        "status": "ok",
        "dry_run": DRY_RUN,
        "allowlist": sorted(ALLOWLIST),
        "cooldown_segundos": COOLDOWN_SEGUNDOS,
    }


if __name__ == "__main__":
    log.info(
        "Remediador iniciado | dry_run=%s allowlist=%s cooldown=%ss",
        DRY_RUN, sorted(ALLOWLIST), COOLDOWN_SEGUNDOS,
    )
    app.run(host="0.0.0.0", port=8080)
