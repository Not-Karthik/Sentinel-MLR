output "service_url" {
  description = "URL Cloud Run reports for the service."
  value       = google_cloud_run_v2_service.sentinel.uri
}

output "expected_app_url" {
  description = "URL passed to the agent as APP_URL (advertised in the A2A agent card)."
  value       = local.app_url
}

output "docs_url" {
  description = "Swagger UI URL (only served when enable_api_docs is true)."
  value       = "${google_cloud_run_v2_service.sentinel.uri}/docs"
}

output "agent_card_url" {
  description = "A2A agent card URL."
  value       = "${google_cloud_run_v2_service.sentinel.uri}/a2a/app/.well-known/agent-card.json"
}

output "image_repository" {
  description = "Artifact Registry path to push images to."
  value       = local.image_base
}

output "media_bucket" {
  description = "Bucket for uploaded media and ADK artifacts."
  value       = google_storage_bucket.media.name
}

output "runtime_service_account" {
  value = google_service_account.runtime.email
}

output "build_service_account" {
  value = google_service_account.build.email
}
