locals {
  apis = [
    "serviceusage.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "storage.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "run.googleapis.com",
    "aiplatform.googleapis.com",
  ]

  media_bucket = coalesce(var.media_bucket_name, "${var.project_id}-sentinel-media")
  app_url      = "https://${var.service_name}-${var.project_number}.${var.region}.run.app"
  image_base   = "${var.region}-docker.pkg.dev/${var.project_id}/${var.repository_id}"

  invokers = toset(concat(
    var.invoker_members,
    var.allow_unauthenticated_invoke ? ["allUsers"] : []
  ))

  env = merge(
    {
      GOOGLE_GENAI_USE_ENTERPRISE = "True"
      GOOGLE_CLOUD_PROJECT        = var.project_id
      GOOGLE_CLOUD_LOCATION       = "global"
      LOGS_BUCKET_NAME            = google_storage_bucket.media.name
      MEDIA_BUCKET_NAME           = google_storage_bucket.media.name
      APP_URL                     = local.app_url
      ENABLE_API_DOCS             = tostring(var.enable_api_docs)
      MEDIA_MAX_BYTES             = tostring(var.media_max_mb * 1024 * 1024)
    },
    var.allow_origins == "" ? {} : { ALLOW_ORIGINS = var.allow_origins },
    var.extra_env
  )
}

resource "google_project_service" "apis" {
  for_each = toset(local.apis)

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "sentinel" {
  project       = var.project_id
  location      = var.region
  repository_id = var.repository_id
  format        = "DOCKER"
  description   = "Sentinel container images"

  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket" "media" {
  project                     = var.project_id
  name                        = local.media_bucket
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = var.bucket_force_destroy

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      days_since_noncurrent_time = 30
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_service_account" "runtime" {
  project      = var.project_id
  account_id   = "${var.service_name}-runtime"
  display_name = "Sentinel Cloud Run runtime"

  depends_on = [google_project_service.apis]
}

resource "google_service_account" "build" {
  project      = var.project_id
  account_id   = "${var.service_name}-build"
  display_name = "Sentinel Cloud Build"

  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "runtime_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_project_iam_member" "runtime_logging" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_storage_bucket_iam_member" "runtime_media" {
  bucket = google_storage_bucket.media.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_project_iam_member" "build_roles" {
  for_each = toset([
    "roles/artifactregistry.writer",
    "roles/logging.logWriter",
    "roles/storage.objectViewer",
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.build.email}"
}

resource "google_cloud_run_v2_service" "sentinel" {
  project             = var.project_id
  name                = var.service_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.deletion_protection

  template {
    service_account                  = google_service_account.runtime.email
    timeout                          = "${var.timeout_seconds}s"
    max_instance_request_concurrency = var.concurrency

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    containers {
      image = var.image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
        cpu_idle          = false
        startup_cpu_boost = true
      }

      startup_probe {
        period_seconds    = 10
        timeout_seconds   = 5
        failure_threshold = 30

        http_get {
          path = "/health"
        }
      }

      dynamic "env" {
        for_each = local.env

        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  lifecycle {
    precondition {
      condition     = var.image != null
      error_message = "Set var.image to the container image to deploy."
    }
  }

  depends_on = [
    google_project_service.apis,
    google_project_iam_member.runtime_vertex,
    google_storage_bucket_iam_member.runtime_media,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "invoker" {
  for_each = local.invokers

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.sentinel.name
  role     = "roles/run.invoker"
  member   = each.value
}
