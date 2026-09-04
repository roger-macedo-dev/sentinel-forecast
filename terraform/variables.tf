variable "projeto" {
  description = "ID do projeto no GCP"
  type        = string
  default     = "sentinel-forecast-507313"
}

variable "zona_primaria" {
  description = "Zona do cluster primario (Global, no vocabulario do design doc)"
  type        = string
  default     = "us-central1-a"
}

variable "zona_secundaria" {
  description = "Zona do cluster secundario (Regiao Restrita, no design doc)"
  type        = string
  default     = "southamerica-east1-a"
}

variable "criar_secundario" {
  description = "Liga o segundo cluster. Separado para nao pagar duas regioes sem necessidade."
  type        = bool
  default     = false
}
