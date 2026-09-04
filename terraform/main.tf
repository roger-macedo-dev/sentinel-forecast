terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source = "hashicorp/google"
      # Fixar a versao maior evita que uma atualizacao do provider mude o
      # comportamento do plan sem aviso.
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.projeto
}

module "cluster_primario" {
  source = "./modules/cluster"

  nome = "sentinel-forecast-cluster"
  zona = var.zona_primaria
}

# Mesma definicao, outra regiao. A duplicacao aqui e so a chamada — a definicao
# do que e um cluster continua unica, dentro do modulo.
module "cluster_secundario" {
  source = "./modules/cluster"
  count  = var.criar_secundario ? 1 : 0

  nome = "sentinel-forecast-cluster-sec"
  zona = var.zona_secundaria
}
