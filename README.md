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
  por métrica a cada minuto e expõe o resultado como `previsao_percentual{alvo}`
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

### Bootstrap (uma vez por cluster)

O RBAC do remediador fica em `k8s/rbac/` e é aplicado **manualmente por quem
administra o cluster**, não pelo pipeline:

```bash
kubectl apply -f k8s/rbac/remediador-rbac.yaml
```

A service account do CD tem `roles/container.developer`, que não permite criar
`Role`/`RoleBinding` — por desenho: um pipeline capaz de conceder permissões pode
escalar o próprio privilégio. Como `kubectl apply -f k8s/` não é recursivo, o
subdiretório `rbac/` fica naturalmente fora do alcance do deploy automatizado.

### Deploy da stack

```bash
kubectl apply -f k8s/
```

A stack inteira roda no cluster:

| Recurso | Observação |
|---|---|
| `node_exporter` | DaemonSet com `hostPath`/`hostNetwork` — monitora o node real, não o container |
| `prometheus` | Deployment + ConfigMap (config e regras de alerta) + PVC para o TSDB, com `strategy: Recreate` (volume `ReadWriteOnce` não admite dois Pods simultâneos) |
| `sidecar` | Deployment, imagem publicada no GHCR pelo CI |
| `grafana` | Deployment com PVC, datasource e dashboard provisionados via código |
| `postgres` | Deployment + PVC, `PGDATA` em subdiretório (disco novo vem com `lost+found`, e o Postgres recusa diretório não-vazio) |
| `alertmanager` | Deployment + ConfigMap, roteamento e inibição por severidade |
| `remediador` | Deployment + ServiceAccount/Role/RoleBinding com permissão mínima (`get`/`patch` em `deployments`) |
| `postgres-exporter` | Deployment, credenciais lidas do Secret |
| `api` | Deployment com `resources` declarados + `HorizontalPodAutoscaler` (1–4 réplicas), credenciais do banco lidas do Secret |

Credenciais do banco ficam em `Secret`; o script de criação dos schemas
multi-tenant é entregue via `ConfigMap` montado em
`/docker-entrypoint-initdb.d`.

Prometheus e Grafana são expostos via `Service type: LoadBalancer` (IP público
gerenciado pela cloud). Banco e API ficam em `ClusterIP` — sem exposição externa.

### Recarga de configuração sem restart desnecessário

`kubectl apply` atualiza um ConfigMap, mas o processo em execução continua com a
config antiga em memória. Em vez de reiniciar tudo a cada deploy, o CD grava um
**checksum da config numa annotation do Pod**: se a config mudou, o template do
Pod muda e o Kubernetes faz rolling update sozinho; se não mudou, nada é
reiniciado.

### Escalonamento automático

A `api` tem `HorizontalPodAutoscaler` (1 a 4 réplicas, alvo de 60% de CPU). Dois
detalhes que decidem se funciona:

- **`resources.requests` é pré-requisito** — o HPA mede utilização como
  percentual do request. Sem request declarado não há denominador, e o
  autoscaler não escala.
- **Redução com janela de estabilização (120s)** — subida imediata, descida
  conservadora. Sem isso, o número de réplicas oscila a cada respiro da carga.

**HPA sozinho tem teto.** Ele cria Pods; não cria capacidade. No teste com carga
real, o autoscaler subiu para 4 réplicas e a quarta ficou `Pending` com
`Insufficient cpu` — o node não tinha espaço. São duas camadas independentes:

| Camada | O que cria | Sem ela |
|---|---|---|
| HorizontalPodAutoscaler | Pods | A aplicação não acompanha a demanda |
| Cluster Autoscaler | Nodes | Os Pods extras ficam `Pending` |

Por isso o cluster é criado com autoscaling de nodes habilitado (ver comando
abaixo). Com as duas camadas ativas, o Pod pendente provocou o provisionamento de
um node novo e passou a rodar.

### Persistência de métricas

O TSDB do Prometheus fica em `PersistentVolumeClaim` (retenção de 15 dias) — sem
isso, todo restart do Pod apaga o histórico e o modelo Prophet precisa acumular
dados do zero antes de voltar a prever. Localmente, o mesmo papel é feito por um
volume nomeado do Docker.

## Dashboard

Versionado como código em `observability/grafana/` e provisionado
automaticamente (datasource + dashboard): um cluster novo já nasce com o
dashboard pronto, sem configuração manual na interface.

Organizado em três seções:

- **Resumo** — as quatro previsões para a próxima hora (memória, CPU, disco,
  conexões), com cor dinâmica por threshold
- **Infraestrutura do node** — painéis de tendência (CPU, Memória, Disco, Rede,
  Load average)
- **Banco de dados (multi-tenant)** — disponibilidade, **saturação de conexões**
  (percentual de `max_connections`, não o número absoluto — é o teto que importa),
  cache hit ratio, conexões por estado (expondo `idle in transaction`, que retém
  locks e trava o autovacuum), taxa de rollback, deadlocks, e **tamanho e dead
  tuples por schema** — ou seja, por cliente
- **Remediação automática** — quantas ações foram executadas, em quais serviços, e
  quantas foram bloqueadas por qual proteção

Critério de formatação: threshold de status só onde existe um teto físico real
(CPU/Memória/Disco em %, cache hit ratio). Rede fica sem threshold, por não ter
capacidade máxima definida, e load average aparece em valor absoluto — deve ser
lido em relação ao número de vCPUs, não como percentual.

## Previsão

As métricas previstas são declaradas em `observability/sidecar/previsoes.yml` —
acrescentar uma previsão é editar YAML, sem tocar em código:

```yaml
previsoes:
  - nome: memoria
    query: '1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)'
    multiplicador: 100
```

Hoje são seis, entre infraestrutura e aplicação:

| Alvo | Origem | Métrica exposta |
|---|---|---|
| memória, CPU, disco | node_exporter | `previsao_percentual{alvo}` |
| saturação de conexões | postgres_exporter | `previsao_percentual{alvo}` |
| taxa de erro da API | aplicação | `previsao_percentual{alvo}` |
| latência p95 da API | aplicação | `previsao_segundos{alvo}` |

Uma métrica com rótulo, não uma métrica por recurso: assim uma única regra de
alerta cobre todos os alvos e o resultado permanece agregável. Latência sai numa
métrica separada porque a unidade é outra — misturar segundos e percentual sob o
mesmo nome inviabilizaria qualquer agregação.

A API é instrumentada com `prometheus_client`, usando o **template** da rota
(`/envios/<cliente>`) como rótulo, nunca o caminho concreto — o caminho criaria
uma série por cliente e a cardinalidade cresceria sem limite.

### Por que crescimento logístico

O Prophet, por padrão, extrapola a tendência recente de forma linear e
**ilimitada**. Na prática isso produziu uma previsão de CPU de **-671%** — o
modelo pegou uma queda brusca de uso e projetou-a por uma hora inteira.

Como uso de recurso é uma grandeza limitada, o modelo é configurado com
crescimento logístico, piso e teto. O limite passa a fazer parte do ajuste, e
não de um corte aplicado depois. Há ainda um corte final como rede de segurança,
porque o intervalo de confiança pode escapar um pouco dos limites.

Efeito colateral conhecido: com tendência de queda numa máquina ociosa, a
previsão satura no piso (0%) em vez de estabilizar num patamar realista.

### Limitações conhecidas

Documentadas porque afetam a leitura dos números, não porque sejam ajustáveis:

- **Histórico curto degrada a previsão.** O modelo treina sobre a última hora. Em
  ambiente recém-provisionado há poucos pontos, e a tendência recente domina.
  Métricas de aplicação (latência, taxa de erro) sofrem mais que as de
  infraestrutura, por serem bem mais voláteis: em teste com ~30 minutos de dados,
  a latência p95 medida era 0,02s e a previsão ficou em 0,9s.
- **Prophet foi desenhado para séries longas**, com sazonalidade diária e semanal.
  Com uma hora de histórico ele só tem tendência para extrapolar — daí a
  flexibilidade de tendência reduzida (`changepoint_prior_scale` baixo), ajustável
  por alvo.
- **Consumo de CPU cresce com o número de alvos.** Cada previsão treina um modelo
  próprio; por isso o intervalo padrão de retreino é de 5 minutos, não 1.

Falha ao prever um alvo não interrompe os demais, e `previsao_falhas{alvo}`
sinaliza quando um modelo para de treinar — sem isso, a métrica ficaria congelada
no último valor sem indicação de problema.

## Alertas

3 regras sobre a métrica **prevista** (não a métrica bruta), em
`observability/prometheus/alert-rules.yml`. As três compartilham o mesmo
`alertname` e se diferenciam pelo rótulo `severity` — é isso que permite ao
Alertmanager tratá-las como o mesmo problema em gravidades diferentes:

| Alerta | Condição | Severidade |
|---|---|---|
| `PrevisaoCapacidade` | previsão > 70% | info |
| `PrevisaoCapacidade` | previsão > 85% | warning |
| `PrevisaoCapacidade` | previsão > 95% | critical |

As regras são escritas sobre `previsao_percentual` sem filtrar o alvo, então
valem automaticamente para qualquer métrica adicionada ao arquivo de previsões.
O rótulo `target` (que aciona a remediação) é aplicado apenas no crítico de
memória, único caso em que existe uma ação automática definida.

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

**ConfigMaps (`configmaps.yml`) — automático**: os arquivos em `k8s/*-configmap.yaml`
são gerados a partir das fontes em `observability/`. O pipeline regenera e falha se
estiverem fora de sincronia, transformando em erro visível o que antes era divergência
silenciosa entre o que está versionado e o que vai para o cluster.

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
por conta de billing), mas nodes cobram por hora.

```bash
gcloud container clusters create sentinel-forecast-cluster \
  --zone us-central1-a \
  --num-nodes 1 --machine-type e2-medium --disk-size 30 \
  --enable-autoscaling --min-nodes 1 --max-nodes 3
```

Ao final de cada sessão, `gcloud container clusters delete`. **Deletar o cluster
não remove os discos dos PVCs** — é preciso conferir `gcloud compute disks list`
e apagá-los, senão continuam sendo cobrados.

## Status

Validado end-to-end em nuvem real (GKE): stack completa no cluster — aplicação
multi-tenant, banco com volume persistente, coleta de métricas, previsão via
Prophet, roteamento de alertas, auto-remediação e dashboard provisionado — com
CI/CD executado de ponta a ponta (build, scan de segurança, publicação das
imagens e deploy no cluster).

O ciclo completo foi exercitado no cluster: alerta crítico disparado →
Alertmanager roteou → webhook → remediador reiniciou o Deployment alvo via API
do Kubernetes → métrica da ação registrada no dashboard.

Também roda inteira localmente via Docker Compose.

## Roadmap

- **Multi-região** — dois clusters GKE em regiões diferentes, replicando a
  separação Global / Região Restrita já descrita em `docs/DESIGN.md`
- **Detecção de anomalia real** — comparar com baseline por horário/dia da semana,
  em vez de extrapolação de tendência contra limiar fixo
- **Janela de treino maior** — treinar sobre dias de histórico, não uma hora, para
  o modelo capturar sazonalidade em vez de só tendência
- **Mais ações de remediação** — hoje só `rollout restart`; adicionar limpeza de
  disco e escalonamento de réplicas como ações possíveis
- **Streaming (Kafka)** — desacoplar API→banco via eventos (mesmo padrão do
  `pedidos-app`); e/ou métricas em tempo real via Kafka Streams/ksqlDB no lugar
  do scrape pull-based do Prometheus
- **Infraestrutura como código** — provisionar o cluster via Terraform, em vez
  de `gcloud` imperativo
