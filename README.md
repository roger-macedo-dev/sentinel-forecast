# Sentinel Forecast

[![CI](https://github.com/roger-macedo-dev/sentinel-forecast/actions/workflows/ci.yml/badge.svg)](https://github.com/roger-macedo-dev/sentinel-forecast/actions/workflows/ci.yml)
[![CD](https://github.com/roger-macedo-dev/sentinel-forecast/actions/workflows/cd.yml/badge.svg)](https://github.com/roger-macedo-dev/sentinel-forecast/actions/workflows/cd.yml)

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
| Auto-remediação | Python, Flask, client oficial do Kubernetes |
| Banco de dados | PostgreSQL 16, multi-tenant (schema por cliente) |
| Forecasting | Python, Flask, Prophet, pandas |
| Observabilidade | Prometheus, Alertmanager, Grafana, node_exporter, postgres_exporter |
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

A stack inteira roda no cluster:

| Recurso | Observação |
|---|---|
| `node_exporter` | DaemonSet com `hostPath`/`hostNetwork` — monitora o node real, não o container |
| `prometheus` | Deployment + ConfigMap (config e regras de alerta) |
| `sidecar` | Deployment, imagem publicada no GHCR pelo CI |
| `grafana` | Deployment com PVC, datasource e dashboard provisionados via código |
| `postgres` | Deployment + PVC, `PGDATA` em subdiretório (disco novo vem com `lost+found`, e o Postgres recusa diretório não-vazio) |
| `alertmanager` | Deployment + ConfigMap, roteamento e inibição por severidade |
| `remediador` | Deployment + ServiceAccount/Role/RoleBinding com permissão mínima (`get`/`patch` em `deployments`) |
| `postgres-exporter` | Deployment, credenciais lidas do Secret |
| `api` | Deployment, credenciais do banco lidas do Secret |

Credenciais do banco ficam em `Secret`; o script de criação dos schemas
multi-tenant é entregue via `ConfigMap` montado em
`/docker-entrypoint-initdb.d`.

Prometheus e Grafana são expostos via `Service type: LoadBalancer` (IP público
gerenciado pela cloud). Banco e API ficam em `ClusterIP` — sem exposição externa.

### Recarga de configuração sem restart desnecessário

`kubectl apply` atualiza um ConfigMap, mas o processo em execução continua com a
config antiga em memória. Em vez de reiniciar tudo a cada deploy (o que apagaria
o histórico do Prometheus), o CD grava um **checksum da config numa annotation do
Pod**: se a config mudou, o template do Pod muda e o Kubernetes faz rolling
update sozinho; se não mudou, nada é reiniciado.

## Dashboard

Versionado como código em `observability/grafana/` e provisionado
automaticamente (datasource + dashboard): um cluster novo já nasce com o
dashboard pronto, sem configuração manual na interface.

Organizado em três seções:

- **Resumo** — Stat panels com os valores atuais (Previsão de memória, CPU,
  Memória), cor dinâmica por threshold
- **Infraestrutura do node** — painéis de tendência (CPU, Memória, Disco, Rede,
  Load average)
- **Banco de dados** — status do Postgres, conexões ativas e cache hit ratio
- **Remediação automática** — quantas ações foram executadas, em quais serviços, e
  quantas foram bloqueadas por qual proteção

Critério de formatação: threshold de status só onde existe um teto físico real
(CPU/Memória/Disco em %, cache hit ratio). Rede fica sem threshold, por não ter
capacidade máxima definida, e load average aparece em valor absoluto — deve ser
lido em relação ao número de vCPUs, não como percentual.

## Alertas

3 regras sobre a métrica **prevista** (não a métrica bruta), em
`observability/prometheus/alert-rules.yml`. As três compartilham o mesmo
`alertname` e se diferenciam pelo rótulo `severity` — é isso que permite ao
Alertmanager tratá-las como o mesmo problema em gravidades diferentes:

| Alerta | Condição | Severidade |
|---|---|---|
| `MemoriaPrevisao` | previsão > 70% | info |
| `MemoriaPrevisao` | previsão > 85% | warning |
| `MemoriaPrevisao` | previsão > 95% | critical |

### Roteamento (Alertmanager)

Configuração em `observability/alertmanager/alertmanager.yml`:

- **Agrupamento** por `alertname` — uma notificação por problema, não uma por instância
- **Urgência diferenciada**: severidade `critical` agrupa mais rápido
  (`group_wait` 10s vs 30s) e reincide mais cedo (`repeat_interval` 1h vs 4h)
- **Inibição em cascata**: enquanto o `critical` estiver disparando, o `warning` e
  o `info` do mesmo alerta são suprimidos — evita três notificações do mesmo
  incidente

Validado injetando as três severidades simultaneamente: apenas o `critical`
permanece ativo, os outros dois entram em `suppressed`.

## Auto-remediação

O receiver `critical` do Alertmanager aponta para o serviço `remediador`
(`remediador/`), que executa a correção automática — hoje, `rollout restart` do
Deployment afetado.

O alvo **não é fixo no código**: vem do rótulo `target` da própria regra de
alerta, então é a regra que declara o que deve ser remediado.

### Proteções

Ação automática mal configurada causa mais dano que o alerta que a originou. Três
guardas, e todas emitem métrica quando bloqueiam:

| Proteção | O que evita |
|---|---|
| **Allowlist** | Reiniciar um serviço que não deveria ser tocado, mesmo que o alerta peça |
| **Cooldown** (10 min por alvo) | Loop de restart quando o alerta persiste depois da ação |
| **Revalidação de severidade** | Agir por engano se o roteamento do Alertmanager for alterado no futuro |

Além disso, `DRY_RUN` controla se a ação é executada ou apenas registrada — no
Docker Compose local roda em dry-run (não há cluster para agir), no Kubernetes
executa de fato.

### Permissões no cluster

O remediador usa uma `ServiceAccount` dedicada com um `Role` de escopo de
namespace concedendo apenas `get` e `patch` sobre `deployments`. Não pode criar,
excluir, ler Secrets nem alcançar outros namespaces: se o serviço for
comprometido, o alcance máximo é reiniciar Deployments do próprio namespace — e a
allowlist restringe ainda mais.

### Observabilidade da própria remediação

`remediacoes_total{alvo,resultado}` e `remediacoes_bloqueadas_total{motivo}` são
expostas ao Prometheus e visualizadas no dashboard. Sem isso, a ação automática
seria uma caixa-preta: volume alto de bloqueios por `cooldown` indica alerta em
loop; por `fora_da_allowlist`, divergência entre a regra de alerta e a
configuração do remediador.

## CI/CD

Dois pipelines separados (`.github/workflows/`), refletindo o fato de o cluster
GKE ser efêmero por design:

**CI (`ci.yml`) — automático**, em todo push e PR contra `main`:

1. Build das imagens `sidecar` e `api` (job em `matrix`, um por serviço)
2. Scan de vulnerabilidade com **Trivy** (`CRITICAL`/`HIGH`), com
   `ignore-unfixed: true` — falha o pipeline apenas quando existe correção
   disponível e não aplicada, em vez de travar indefinidamente por CVE de
   pacote do SO ainda sem patch upstream
3. Push pro GHCR com tag = SHA do commit (rastreável, nunca sobrescreve)

Em PR o pipeline builda e escaneia mas **não publica** — só push em `main` publica.

**CD (`cd.yml`) — manual** (`workflow_dispatch`, com a tag da imagem como input):
autentica no GCP por service account dedicada (`roles/container.developer`,
menor privilégio), obtém credenciais do cluster, injeta a tag escolhida no
manifest e roda `kubectl apply -f k8s/`. Manual porque o cluster só existe
durante as sessões de teste.

### Hardening aplicado no processo

O scan encontrou CVEs reais no toolchain de build (`pip`, `setuptools`,
`ensurepip`, e libs vendorizadas dentro do próprio `pip`). Correção adotada:
**remover pip/setuptools/ensurepip da imagem final** — são ferramentas de
build, não de runtime; as dependências já chegam prontas do estágio `builder`.
Reduz superfície de ataque de verdade, em vez de suprimir o alerta.

## Documentação

- [Design de arquitetura](docs/DESIGN.md) — cenário, opções avaliadas, decisão

## Custo e disciplina de uso

Cluster GKE não fica ativo permanentemente — control plane é isento (1 cluster
por conta de billing), mas nodes cobram por hora. Fluxo de trabalho: provisiona
(`gcloud container clusters create`), testa, deleta (`gcloud container clusters
delete`) ao final de cada sessão.

## Status

Validado end-to-end em nuvem real (GKE): stack completa no cluster — aplicação
multi-tenant, banco com volume persistente, coleta de métricas, previsão via
Prophet, alertas e dashboard provisionado — com CI/CD executado de ponta a ponta
(build, scan de segurança, publicação das imagens e deploy no cluster).

Também roda inteira localmente via Docker Compose.

## Roadmap

- **Multi-região** — dois clusters GKE em regiões diferentes, replicando a
  separação Global / Região Restrita já descrita em `docs/DESIGN.md`
- **Mais previsão de falha** — Prophet em outras métricas (CPU, disco, conexões
  do Postgres, latência da API), detecção de anomalia real vs. limiar fixo,
  alertas preditivos no lado da aplicação (taxa de erro crescendo)
- **Mais ações de remediação** — hoje só `rollout restart`; adicionar limpeza de
  disco e escalonamento de réplicas como ações possíveis
- **HPA / autoscaling** — escalonamento automático de réplicas
- **Persistência do Prometheus** — hoje o histórico de métricas é efêmero;
  adicionar PVC para sobreviver a restart do Pod
- **Streaming (Kafka)** — desacoplar API→banco via eventos (mesmo padrão do
  `pedidos-app`); e/ou métricas em tempo real via Kafka Streams/ksqlDB no lugar
  do scrape pull-based do Prometheus
- **Infraestrutura como código** — provisionar o cluster via Terraform, em vez
  de `gcloud` imperativo
