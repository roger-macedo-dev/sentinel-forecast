# Sentinel Forecast

Plataforma de observabilidade preditiva para ambientes Kubernetes — um sidecar de
forecasting (Prophet) integrado à stack Prometheus/Grafana, antecipando degradação
de performance antes que vire incidente, com alertas segmentados por criticidade.

## O problema

Monitoramento tradicional é **reativo**: alertas disparam quando o limiar já foi
ultrapassado, não antes. Este projeto adiciona uma camada **preditiva** sobre uma
stack de observabilidade padrão, usando um modelo de séries temporais (Prophet)
treinado continuamente sobre métricas já coletadas pelo Prometheus.

Cenário completo e opções de arquitetura avaliadas em [`docs/DESIGN.md`](docs/DESIGN.md).

## Arquitetura

```
node_exporter (DaemonSet)  ->  Prometheus  <->  sidecar (Prophet)
postgres_exporter          ->      |                 ^
                                Grafana          expõe /metrics
api (multi-tenant) -> postgres (dashboards)   (previsão computada)
```

- `node_exporter` coleta métricas reais do node (CPU, memória, disco, rede)
- `postgres` banco real, **multi-tenant**: cada cliente é um schema isolado
  (`cliente_norte`, `cliente_sul`, `cliente_leste`), mesmo padrão descrito no
  cenário de origem do projeto
- `api` aplicação Flask mínima, gera tráfego real de escrita/leitura no banco,
  roteando dinamicamente pro schema do tenant correto
- `postgres_exporter` traduz estatísticas internas do Postgres (conexões,
  cache hit ratio) em métricas Prometheus
- `sidecar` consulta o histórico via API do Prometheus, treina um modelo Prophet
  a cada minuto, expõe a previsão como métrica nova (`memoria_previsao_percentual`)
- `Prometheus` faz scrape de volta dessa métrica computada — o ciclo se fecha
- Alertas segmentados por criticidade (info/warning/critical) disparam sobre a
  métrica **prevista**, não a métrica bruta
- `Grafana` visualiza tudo: painéis de resumo (Stat, números grandes com cor por
  status) + painéis de tendência (Time series) organizados por seção

## Stack técnica

| Camada | Tecnologia |
|---|---|
| Aplicação | Python, Flask |
| Banco de dados | PostgreSQL 16, multi-tenant (schema por cliente) |
| Forecasting | Python, Flask, Prophet, pandas |
| Observabilidade | Prometheus, Grafana, node_exporter, postgres_exporter |
| Orquestração | Kubernetes (testado em GKE) |
| IaC / Deploy | Docker Compose (local), manifests Kubernetes (`k8s/`) |
| Cloud | Google Cloud Platform |

## Rodando localmente (Docker Compose)

```bash
docker compose up -d
```

Serviços disponíveis:

| Serviço | URL |
|---|---|
| API | http://localhost:5000 |
| Prometheus | http://localhost:9091 |
| Sidecar (métricas) | http://localhost:8000/metrics |

### Testando a API multi-tenant

```bash
curl -X POST http://localhost:5000/envios/norte \
  -H "Content-Type: application/json" \
  -d '{"destino": "Sao Paulo"}'

curl http://localhost:5000/envios/norte
```

Tenants disponíveis: `norte`, `sul`, `leste` — cada um isolado em seu próprio
schema no Postgres (`cliente_norte`, `cliente_sul`, `cliente_leste`).

## Deploy no Kubernetes (GKE)

```bash
kubectl apply -f k8s/
```

Cria: `node_exporter` (DaemonSet, com acesso ao host via `hostPath`/`hostNetwork`
para monitorar o node real, não o container), `prometheus` (Deployment +
ConfigMap para config/regras de alerta), `sidecar` (Deployment, imagem publicada
no GHCR), `grafana` (Deployment com `PersistentVolumeClaim` para não perder
dashboards/datasources em caso de restart do Pod).

> Nota: `api` e `postgres` (multi-tenant) ainda existem só no Docker Compose local
> — deploy desses dois no Kubernetes é o próximo passo do backlog.

Prometheus e Grafana são expostos via `Service type: LoadBalancer` (IP público
gerenciado pela cloud).

## Dashboard

Organizado em duas seções (rows):

- **Resumo** — Stat panels com valores atuais (Previsão de Memória, CPU, Memória),
  cor dinâmica por threshold (verde/amarelo/vermelho)
- **Infraestrutura do Node** — painéis de tendência (CPU, Memória, Disco, Rede,
  Load average), com thresholds aplicados onde fazem sentido fisicamente (CPU/
  Memória/Disco em %, Load average em valor absoluto relativo ao número de
  vCPUs — rede sem threshold, por não ter teto de capacidade definido)

## Alertas

3 regras segmentadas por criticidade, sobre a métrica prevista
(`observability/prometheus/alert-rules.yml`):

| Alerta | Condição | Severidade |
|---|---|---|
| `MemoriaPrevisaoInfo` | previsão > 70% | info |
| `MemoriaPrevisaoWarning` | previsão > 85% | warning |
| `MemoriaPrevisaoCritical` | previsão > 95% | critical |

## Documentação

- [Design de arquitetura](docs/DESIGN.md) — cenário, opções avaliadas, decisão

## Custo e disciplina de uso

Cluster GKE não fica ativo permanentemente — control plane é isento (1 cluster
por conta de billing), mas nodes cobram por hora. Fluxo de trabalho: provisiona
(`gcloud container clusters create`), testa, deleta (`gcloud container clusters
delete`) ao final de cada sessão.

## Status

Prova de conceito validada end-to-end: local (Docker Compose) e em nuvem real
(GKE, para node_exporter/prometheus/sidecar/grafana). Backlog: deploy de
api/postgres no Kubernetes, multi-região (replicar a separação de ambientes
descrita no design doc), HPA/autoscaling, réplicas do sidecar por
squad/namespace.
