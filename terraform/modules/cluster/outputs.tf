output "nome" {
  value = google_container_cluster.principal.name
}

output "zona" {
  value = google_container_cluster.principal.location
}

output "endpoint" {
  value     = google_container_cluster.principal.endpoint
  sensitive = true
}
