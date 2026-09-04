resource "google_container_cluster" "principal" {
  name     = var.nome
  location = var.zona

  # O node pool padrao nao pode ser alterado sem recriar o cluster inteiro.
  # Removendo-o e gerenciando um pool proprio, da para mudar tipo de maquina,
  # autoscaling e versao sem destruir o cluster.
  remove_default_node_pool = true
  initial_node_count       = 1

  # Por padrao o provider protege o cluster contra destruicao. Aqui o cluster e
  # efemero por decisao de custo, entao a protecao atrapalharia o `destroy`.
  deletion_protection = false
}

resource "google_container_node_pool" "principal" {
  name     = "${var.nome}-pool"
  cluster  = google_container_cluster.principal.name
  location = var.zona

  autoscaling {
    min_node_count = var.min_nodes
    max_node_count = var.max_nodes
  }

  node_config {
    machine_type = var.tipo_maquina
    disk_size_gb = var.disco_gb
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }
}
