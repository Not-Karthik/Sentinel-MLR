variable "project_id" {
  description = "Existing GCP project that hosts Sentinel."
  type        = string
}

variable "project_number" {
  description = "Numeric project number of project_id (the deploy script looks it up)."
  type        = string

  validation {
    condition     = can(regex("^[0-9]+$", var.project_number))
    error_message = "project_number must be numeric."
  }
}

variable "region" {
  description = "Region for Cloud Run, Artifact Registry and the media bucket."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Cloud Run service name."
  type        = string
  default     = "sentinel"
}

variable "repository_id" {
  description = "Artifact Registry Docker repository id."
  type        = string
  default     = "sentinel"
}

variable "image" {
  description = "Full container image reference. Required for the Cloud Run service."
  type        = string
  default     = null
}

variable "media_bucket_name" {
  description = "Bucket for uploaded media and ADK artifacts. Defaults to <project_id>-sentinel-media."
  type        = string
  default     = null
}

variable "bucket_force_destroy" {
  description = "Allow terraform destroy to delete the media bucket even when it has objects."
  type        = bool
  default     = false
}

variable "allow_unauthenticated_invoke" {
  description = "Grant allUsers the Cloud Run invoker role so anyone can call the service."
  type        = bool
  default     = true
}

variable "invoker_members" {
  description = "Extra IAM members granted roles/run.invoker, for example user:name@example.com."
  type        = list(string)
  default     = []
}

variable "enable_api_docs" {
  description = "Serve Swagger UI at /docs and the OpenAPI schema at /openapi.json."
  type        = bool
  default     = true
}

variable "allow_origins" {
  description = "Comma separated CORS origins for the API. Empty disables the CORS middleware."
  type        = string
  default     = ""
}

variable "media_max_mb" {
  description = "Maximum upload size in MiB for POST /api/v1/media. Cloud Run rejects requests above 32 MiB."
  type        = number
  default     = 32

  validation {
    condition     = var.media_max_mb >= 1 && var.media_max_mb <= 32
    error_message = "media_max_mb must be between 1 and 32."
  }
}

variable "extra_env" {
  description = "Additional environment variables for the container."
  type        = map(string)
  default     = {}
}

variable "cpu" {
  description = "CPU limit for the container."
  type        = string
  default     = "2"
}

variable "memory" {
  description = "Memory limit for the container."
  type        = string
  default     = "4Gi"
}

variable "min_instances" {
  description = "Minimum Cloud Run instances. Sessions and A2A tasks are in memory, so keep this at 1."
  type        = number
  default     = 1
}

variable "max_instances" {
  description = "Maximum Cloud Run instances. Keep at 1 while sessions and A2A tasks are in memory."
  type        = number
  default     = 1
}

variable "concurrency" {
  description = "Maximum concurrent requests per instance."
  type        = number
  default     = 20
}

variable "timeout_seconds" {
  description = "Cloud Run request timeout. Streaming agent runs can take several minutes."
  type        = number
  default     = 3600
}

variable "deletion_protection" {
  description = "Protect the Cloud Run service from terraform destroy."
  type        = bool
  default     = true
}
