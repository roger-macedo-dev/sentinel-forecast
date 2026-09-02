# Sentinel Forecast — Design de Arquitetura

## Contexto

Plataforma SaaS multi-tenant de gestão logística, operando em múltiplas regiões
geográficas (grupo "Global" + um grupo de região com exigência regulatória local de
residência de dados), com infraestrutura em Kubernetes e deploy via GitOps.

## Problema

O monitoramento existente (Prometheus + Grafana + Loki) é inteiramente **reativo** —
alertas disparam quando o problema já está acontecendo (limiar estático de CPU/memória
ultrapassado, serviço fora do ar). Isso gera:

- Perda de tempo entre o incidente ocorrer e o time ser notificado
- Ausência de segmentação de criticidade nos alertas (tudo tem o mesmo peso)
- Falta de padronização de health-check entre serviços
- Nenhum mapeamento de dependências críticas externas

## Distinção importante: Multi-Zone vs Multi-Site

Durante a análise, dois problemas de resiliência distintos foram identificados e não
devem ser confundidos:

- **Multi-Zone (Regional):** banco de dados principal roda em zona única (single
  zone) — ponto único de falha dentro da própria região. Correção: réplica síncrona
  em segunda zona, failover automático (equivalente ao Multi-AZ do RDS na AWS).
  Escopo: resiliência a falha de datacenter.
- **Multi-Site (Multi-Região):** dois grupos de ambiente inteiros, cada um numa
  região geográfica distinta, com stacks de infraestrutura independentes. Resolve
  residência de dados e latência geográfica — não é redundância, são sistemas
  separados. Escopo muito maior de custo/complexidade.

O documento de contexto original recomendava migração para multi-zone no banco
principal — problema proporcional à solução, sem necessidade de ir direto para
multi-site.

## Opções de solução avaliadas

**Opção A — Evolução incremental (Prometheus nativo)**
Padroniza health-check, segmenta alertas por criticidade (info/warning/critical),
usa `predict_linear` do PromQL para tendências simples. Baixo custo, mas ainda
limitado a extrapolação linear.

**Opção B — Observabilidade preditiva com ML (escolhida)**
Sidecar Python por squad/namespace, consultando Prometheus periodicamente, treinando
forecasting com Prophet (captura sazonalidade e tendência, não só reta), expondo
previsão como métrica nova via endpoint `/metrics` — mantém modelo pull-based,
100% compatível com a stack existente.

**Opção C — Plataforma gerenciada (Datadog/CloudWatch Anomaly Detection)**
Zero código próprio, mas vendor lock-in e custo recorrente maior.

## Decisão

Opção B. Justificativa: mais robusto que extrapolação linear, mantém controle e
portabilidade (sem vendor lock-in), esforço de implementação proporcional ao ganho
real de sinal preditivo.

## Arquitetura do sidecar

1. Consulta periódica ao Prometheus via API (`/api/v1/query_range`)
2. Treinamento de modelo Prophet por métrica monitorada
3. Exposição do resultado como métrica Prometheus (`/metrics` próprio)
4. Prometheus faz scrape normal — sem Pushgateway, sem mudança na stack existente
5. Alertmanager dispara regras sobre a métrica computada, segmentadas por
   criticidade (info/warning/critical)

## Status de implementacao (atualizado)

Multi-tenant e a aplicacao real ja foram implementados: Postgres com 3 schemas
(cliente_norte/sul/leste), API Flask minima gerando trafego real, `postgres_exporter`
integrado ao Prometheus. Validado local via Docker Compose. Deploy desses componentes
no Kubernetes e a separacao Multi-Site (Global vs Regiao Restrita) permanecem como
proximos passos.
