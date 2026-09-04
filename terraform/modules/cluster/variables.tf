variable "nome" {
  description = "Nome do cluster"
  type        = string
}

variable "zona" {
  description = "Zona onde o cluster e criado (zonal, nao regional: mais barato)"
  type        = string
}

variable "tipo_maquina" {
  description = "Tipo de maquina dos nodes"
  type        = string
  default     = "e2-medium"
}

variable "disco_gb" {
  description = "Tamanho do disco de cada node"
  type        = number
  default     = 30
}

variable "min_nodes" {
  type    = number
  default = 1
}

variable "max_nodes" {
  type    = number
  default = 3
}
