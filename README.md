# Sentinel Forecast

Plataforma de observabilidade preditiva para ambientes Kubernetes multi-região —
um sidecar de forecasting (Prophet) integrado à stack Prometheus/Grafana existente,
antecipando degradação de performance antes que vire incidente.

## O problema

Monitoramento tradicional é reativo: alertas disparam quando o limiar já foi
ultrapassado, não antes. Este projeto adiciona uma camada preditiva sobre a stack
de observabilidade já validada em outros projetos deste portfólio
([ansible-infra-observability](https://github.com/roger-macedo-dev/ansible-infra-observability),
[comments-api-aws-observability](https://github.com/roger-macedo-dev/comments-api-aws-observability)).

## Stack técnica

| Camada | Tecnologia |
|---|---|
| Forecasting | Python + Prophet |
| Observabilidade | Prometheus, Grafana, Alertmanager |
| Orquestração | Kubernetes |
| Cloud | Google Cloud Platform (GKE) |

## Documentação

- [Design de arquitetura](docs/DESIGN.md) — cenário, opções avaliadas, decisão

## Status

Em desenvolvimento.
