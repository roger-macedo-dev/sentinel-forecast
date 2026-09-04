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

# Allowlist por alvo E por acao: "sidecar:reiniciar|limpar_pods_falhos,api:reiniciar".
# Sem ":" o alvo aceita qualquer acao. Controlar apenas o alvo nao basta —
# escalar o sidecar, por exemplo, "funciona", mas duplica o treino dos modelos e
# faz duas replicas publicarem previsoes divergentes para a mesma metrica.
def _carregar_allowlist(bruto):
    permitido = {}
    for entrada in bruto.split(","):
        entrada = entrada.strip()
        if not entrada:
            continue
        if ":" in entrada:
            alvo, acoes = entrada.split(":", 1)
            permitido[alvo.strip()] = {a.strip() for a in acoes.split("|") if a.strip()}
        else:
            permitido[entrada] = None  # None = todas as acoes
    return permitido


ALLOWLIST = _carregar_allowlist(
    os.environ.get("ALLOWLIST", "sidecar:reiniciar|limpar_pods_falhos,api:reiniciar|escalar")
)

# Cooldown: tempo minimo entre duas remediacoes do mesmo alvo.
# Sem isso, um alerta que persiste apos a acao viraria loop.
COOLDOWN_SEGUNDOS = int(os.environ.get("COOLDOWN_SEGUNDOS", "600"))

# Teto de replicas para a acao de escalonamento.
MAX_REPLICAS = int(os.environ.get("MAX_REPLICAS", "4"))

NAMESPACE = os.environ.get("NAMESPACE", "default")

ACOES_VALIDAS = {"reiniciar", "escalar", "limpar_pods_falhos"}

# --- Metricas -----------------------------------------------------------------
remediacoes = Counter(
    "remediacoes_total",
    "Remediacoes executadas pelo remediador",
    ["alvo", "acao", "resultado"],
)
bloqueios = Counter(
    "remediacoes_bloqueadas_total",
    "Remediacoes recusadas pelas protecoes",
    ["motivo"],
)

# Ultima remediacao por alvo, para o controle de cooldown.
ultima_execucao = {}


def clientes():
    from kubernetes import client, config

    config.load_incluster_config()
    return client.AppsV1Api(), client.CoreV1Api(), client.AutoscalingV1Api()


def gerenciado_por_hpa(autoscaling, alvo):
    """Verifica se algum HorizontalPodAutoscaler aponta para este Deployment.

    Escalonar manualmente um alvo gerenciado por HPA e inutil: o autoscaler
    sobrescreve o valor na proxima avaliacao. Pior, o remediador registraria
    sucesso sem que nada mudasse de fato."""
    hpas = autoscaling.list_namespaced_horizontal_pod_autoscaler(namespace=NAMESPACE)
    for hpa in hpas.items:
        alvo_hpa = hpa.spec.scale_target_ref
        if alvo_hpa.kind == "Deployment" and alvo_hpa.name == alvo:
            return True
    return False


def acao_reiniciar(alvo):
    """Equivalente ao `kubectl rollout restart deployment/<alvo>`: grava uma
    annotation com o horario atual no template do Pod, o que muda o template e
    faz o Kubernetes conduzir um rolling update."""
    apps, _, _ = clientes()
    corpo = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "remediador/reiniciado-em": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                        )
                    }
                }
            }
        }
    }
    apps.patch_namespaced_deployment(name=alvo, namespace=NAMESPACE, body=corpo)
    return f"reiniciado: '{alvo}'"


def acao_escalar(alvo):
    apps, _, autoscaling = clientes()

    if gerenciado_por_hpa(autoscaling, alvo):
        raise PermissionError(
            f"'{alvo}' e gerenciado por HPA; escalonamento manual seria sobrescrito"
        )

    atual = apps.read_namespaced_deployment(name=alvo, namespace=NAMESPACE)
    replicas_atuais = atual.spec.replicas or 1

    if replicas_atuais >= MAX_REPLICAS:
        raise ValueError(f"'{alvo}' ja esta no teto de {MAX_REPLICAS} replicas")

    novas = replicas_atuais + 1
    apps.patch_namespaced_deployment(
        name=alvo, namespace=NAMESPACE, body={"spec": {"replicas": novas}}
    )
    return f"escalado: '{alvo}' de {replicas_atuais} para {novas} replicas"


def acao_limpar_pods_falhos(alvo):
    """Remove Pods que terminaram em falha (inclui os despejados por falta de
    recurso no node). Pod nesse estado nao volta sozinho e ocupa espaco na
    listagem, escondendo problemas reais."""
    _, core, _ = clientes()
    pods = core.list_namespaced_pod(namespace=NAMESPACE, label_selector=f"app={alvo}")

    removidos = 0
    for pod in pods.items:
        if pod.status.phase == "Failed":
            core.delete_namespaced_pod(name=pod.metadata.name, namespace=NAMESPACE)
            removidos += 1

    return f"limpeza de '{alvo}': {removidos} Pod(s) em falha removido(s)"


EXECUTORES = {
    "reiniciar": acao_reiniciar,
    "escalar": acao_escalar,
    "limpar_pods_falhos": acao_limpar_pods_falhos,
}


def processar(alerta):
    labels = alerta.get("labels", {})
    alvo = labels.get("target")
    severidade = labels.get("severity")
    acao = labels.get("acao", "reiniciar")

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

    acoes_permitidas = ALLOWLIST[alvo]
    if acoes_permitidas is not None and acao not in acoes_permitidas:
        bloqueios.labels(motivo="acao_nao_permitida_no_alvo").inc()
        log.warning(
            "Acao '%s' nao permitida em '%s' (permitidas: %s)",
            acao, alvo, sorted(acoes_permitidas),
        )
        return f"recusado: acao '{acao}' nao permitida em '{alvo}'"

    if acao not in ACOES_VALIDAS:
        bloqueios.labels(motivo="acao_desconhecida").inc()
        log.warning("Acao '%s' desconhecida", acao)
        return f"recusado: acao '{acao}' desconhecida"

    agora = time.time()
    anterior = ultima_execucao.get(alvo)
    if anterior and (agora - anterior) < COOLDOWN_SEGUNDOS:
        restante = int(COOLDOWN_SEGUNDOS - (agora - anterior))
        bloqueios.labels(motivo="cooldown").inc()
        log.info("Alvo '%s' em cooldown, faltam %ss", alvo, restante)
        return f"adiado: cooldown ativo, faltam {restante}s"

    if DRY_RUN:
        ultima_execucao[alvo] = agora
        remediacoes.labels(alvo=alvo, acao=acao, resultado="dry_run").inc()
        log.info("[DRY-RUN] executaria '%s' em '%s'", acao, alvo)
        return f"dry-run: executaria '{acao}' em '{alvo}'"

    try:
        resultado = EXECUTORES[acao](alvo)
    except PermissionError as erro:
        # Condicao esperada (ex: alvo gerenciado por HPA), nao falha de execucao.
        bloqueios.labels(motivo="conflito_com_hpa").inc()
        log.warning("Acao '%s' recusada em '%s': %s", acao, alvo, erro)
        return f"recusado: {erro}"
    except Exception as erro:
        remediacoes.labels(alvo=alvo, acao=acao, resultado="erro").inc()
        log.error("Falha ao executar '%s' em '%s': %s", acao, alvo, erro)
        return f"erro ao executar '{acao}' em '{alvo}': {erro}"

    ultima_execucao[alvo] = agora
    remediacoes.labels(alvo=alvo, acao=acao, resultado="sucesso").inc()
    log.info("Remediacao automatica -> %s", resultado)
    return resultado


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
        "allowlist": {a: (sorted(v) if v else "todas") for a, v in ALLOWLIST.items()},
        "cooldown_segundos": COOLDOWN_SEGUNDOS,
        "max_replicas": MAX_REPLICAS,
        "acoes": sorted(ACOES_VALIDAS),
    }


if __name__ == "__main__":
    log.info(
        "Remediador iniciado | dry_run=%s allowlist=%s cooldown=%ss acoes=%s",
        DRY_RUN, ALLOWLIST, COOLDOWN_SEGUNDOS, sorted(ACOES_VALIDAS),
    )
    app.run(host="0.0.0.0", port=8080)
