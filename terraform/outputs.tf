output "cluster_primario" {
  value = {
    nome = module.cluster_primario.nome
    zona = module.cluster_primario.zona
  }
}

output "cluster_secundario" {
  value = var.criar_secundario ? {
    nome = module.cluster_secundario[0].nome
    zona = module.cluster_secundario[0].zona
  } : null
}
