[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'deploy.env'),
    [switch]$PlanOnly,
    [switch]$SkipBuild,
    [switch]$Destroy,
    [string]$Image
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$TerraformVersion = '1.13.5'
$ServiceName = 'sentinel'
$RepositoryId = 'sentinel'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$TfDir = Join-Path $Root 'terraform-cloudrun'
$ToolsDir = Join-Path $Root '.tools'
$VarFile = Join-Path $ToolsDir 'generated.tfvars.json'
$PlanFile = Join-Path $ToolsDir 'sentinel.tfplan'

function Fail([string]$Message) {
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

function Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Read-Config([string]$Path) {
    if (-not (Test-Path $Path)) {
        Fail "Config file not found: $Path. Copy deploy\deploy.env.example to deploy\deploy.env and edit it."
    }
    $values = @{}
    foreach ($line in Get-Content $Path) {
        $trimmed = $line.Trim()
        if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
        $index = $trimmed.IndexOf('=')
        if ($index -lt 1) { Fail "Invalid line in ${Path}: $line" }
        $value = $trimmed.Substring($index + 1).Trim()
        if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2).Trim()
        }
        $values[$trimmed.Substring(0, $index).Trim()] = $value
    }
    return $values
}

function Get-Setting($Config, [string]$Name, [string]$Default) {
    if ($Config.ContainsKey($Name) -and $Config[$Name] -ne '') { return $Config[$Name] }
    return $Default
}

function ConvertTo-Bool([string]$Value, [string]$Name) {
    switch ($Value.ToLower()) {
        { $_ -in 'true', '1', 'yes' } { return $true }
        { $_ -in 'false', '0', 'no' } { return $false }
        default { Fail "$Name must be true or false, got '$Value'." }
    }
}

function Test-Gcloud {
    if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
        Fail 'gcloud is not installed. Install it from https://cloud.google.com/sdk/docs/install and run: gcloud auth login'
    }
    $null = & gcloud --version 2>$null
    if ($LASTEXITCODE -ne 0) {
        Fail 'gcloud failed to start. If it reports a Python error, set CLOUDSDK_PYTHON to a working Python 3.10-3.14 executable, or reinstall the Google Cloud CLI.'
    }
    $account = & gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>$null
    if (-not $account) {
        Fail 'No active gcloud account. Run: gcloud auth login'
    }
    return ($account | Select-Object -First 1)
}

function Install-Terraform {
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'amd64' }
    $dir = Join-Path $ToolsDir "terraform-$TerraformVersion"
    $exe = Join-Path $dir 'terraform.exe'
    if (Test-Path $exe) { return $exe }
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $baseUrl = "https://releases.hashicorp.com/terraform/$TerraformVersion"
    $zipName = "terraform_${TerraformVersion}_windows_$arch.zip"
    $zipPath = Join-Path $dir $zipName
    $sumsPath = Join-Path $dir 'SHA256SUMS'
    Write-Host "Downloading Terraform $TerraformVersion ($arch)"
    Invoke-WebRequest -UseBasicParsing -ErrorAction Stop -Uri "$baseUrl/$zipName" -OutFile $zipPath
    Invoke-WebRequest -UseBasicParsing -ErrorAction Stop -Uri "$baseUrl/terraform_${TerraformVersion}_SHA256SUMS" -OutFile $sumsPath
    $expected = (Get-Content $sumsPath | Where-Object { $_ -match [regex]::Escape($zipName) } | ForEach-Object { ($_ -split '\s+')[0] } | Select-Object -First 1)
    if (-not $expected) { Fail "No checksum found for $zipName." }
    $actual = (Get-FileHash -Algorithm SHA256 -Path $zipPath).Hash.ToLower()
    if ($actual -ne $expected.ToLower()) {
        Remove-Item -Force $zipPath
        Fail "Checksum mismatch for $zipName."
    }
    Expand-Archive -Force -ErrorAction Stop -Path $zipPath -DestinationPath $dir
    Remove-Item -Force $zipPath
    if (-not (Test-Path $exe)) { Fail 'Terraform binary missing after extraction.' }
    return $exe
}

function Set-AccessToken {
    $token = & gcloud auth print-access-token 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $token) { Fail 'Could not get a gcloud access token. Run: gcloud auth login' }
    $env:GOOGLE_OAUTH_ACCESS_TOKEN = ($token | Select-Object -First 1)
}

function Invoke-Terraform {
    param([string[]]$Arguments)
    Set-AccessToken
    & $script:Terraform "-chdir=$TfDir" @Arguments | Out-Host
    return $LASTEXITCODE
}

function Invoke-WithRetry([scriptblock]$Action, [string]$What) {
    $attempts = 4
    for ($attempt = 1; $attempt -le $attempts; $attempt++) {
        $code = & $Action
        if ($code -eq 0) { return }
        if ($attempt -lt $attempts) {
            Write-Host "$What failed (attempt $attempt of $attempts); retrying in 60 seconds (API and IAM changes can take a few minutes to propagate)." -ForegroundColor Yellow
            Start-Sleep -Seconds 60
        }
    }
    Fail "$What failed after $attempts attempts. Re-running the script is safe and resumes from here."
}

function Write-VarFile([hashtable]$Values) {
    New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
    $json = $Values | ConvertTo-Json -Depth 5
    [System.IO.File]::WriteAllText($VarFile, $json, (New-Object System.Text.UTF8Encoding $false))
}

function Invoke-Probe([string]$Url, [hashtable]$Headers) {
    try {
        return Invoke-WebRequest -UseBasicParsing -Uri $Url -Headers $Headers -TimeoutSec 30
    } catch {
        return $null
    }
}

$config = Read-Config $ConfigPath
$projectId = Get-Setting $config 'PROJECT_ID' ''
if ($projectId -eq '' -or $projectId -eq 'your-gcp-project-id') { Fail 'Set PROJECT_ID in the config file.' }
if ($projectId -notmatch '^[a-z][a-z0-9-]{4,28}[a-z0-9]$') { Fail "PROJECT_ID '$projectId' is not a valid project id (lowercase letters, digits and hyphens, 6 to 30 characters). Use the ID column from the console, without quotes." }
$region = Get-Setting $config 'REGION' 'us-central1'
$allowPublic = ConvertTo-Bool (Get-Setting $config 'ALLOW_UNAUTHENTICATED_INVOKE' 'true') 'ALLOW_UNAUTHENTICATED_INVOKE'
$enableDocs = ConvertTo-Bool (Get-Setting $config 'ENABLE_API_DOCS' 'true') 'ENABLE_API_DOCS'
$deletionProtection = ConvertTo-Bool (Get-Setting $config 'DELETION_PROTECTION' 'true') 'DELETION_PROTECTION'
$allowOrigins = Get-Setting $config 'ALLOW_ORIGINS' ''
$mediaMaxMbText = Get-Setting $config 'MEDIA_MAX_MB' '32'
if ($mediaMaxMbText -notmatch '^[0-9]+$' -or [int]$mediaMaxMbText -lt 1 -or [int]$mediaMaxMbText -gt 32) { Fail "MEDIA_MAX_MB must be a whole number from 1 to 32, got '$mediaMaxMbText'." }
$mediaMaxMb = [int]$mediaMaxMbText
$invokers = [string[]]@((Get-Setting $config 'INVOKER_MEMBERS' '') -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' })

Step 'Checking gcloud'
$account = Test-Gcloud
Write-Host "Using gcloud account $account and project $projectId"

$projectNumber = & gcloud projects describe $projectId --format='value(projectNumber)' 2>$null
if ($LASTEXITCODE -ne 0 -or -not $projectNumber) { Fail "Cannot read project '$projectId'. Check the project id and your access." }
$projectNumber = ($projectNumber | Select-Object -First 1).Trim()

$billing = & gcloud billing projects describe $projectId --format='value(billingEnabled)' 2>$null
if ($LASTEXITCODE -eq 0 -and $billing -and ($billing | Select-Object -First 1).Trim().ToLower() -ne 'true') {
    Fail "Billing is not enabled on project '$projectId'. Link a billing account and re-run."
}

if ($allowPublic) {
    Write-Host 'WARNING: ALLOW_UNAUTHENTICATED_INVOKE=true. Anyone with the URL can call this service and use your Gemini quota.' -ForegroundColor Yellow
}

Step 'Preparing Terraform'
$script:Terraform = Install-Terraform
Write-Host "Terraform: $script:Terraform"

Step 'Enabling bootstrap APIs'
& gcloud services enable serviceusage.googleapis.com cloudresourcemanager.googleapis.com storage.googleapis.com --project $projectId
if ($LASTEXITCODE -ne 0) { Fail 'Could not enable bootstrap APIs.' }

$stateBucket = "$projectId-sentinel-tfstate"
$null = & gcloud storage buckets describe "gs://$stateBucket" --project $projectId 2>$null
if ($LASTEXITCODE -ne 0) {
    Step "Creating Terraform state bucket gs://$stateBucket"
    & gcloud storage buckets create "gs://$stateBucket" --project $projectId --location $region --uniform-bucket-level-access --public-access-prevention
    if ($LASTEXITCODE -ne 0) { Fail 'Could not create the state bucket.' }
    & gcloud storage buckets update "gs://$stateBucket" --project $projectId --versioning
    if ($LASTEXITCODE -ne 0) { Fail 'Could not enable versioning on the state bucket.' }
}

$imageBase = "$region-docker.pkg.dev/$projectId/$RepositoryId"
$buildServiceAccount = "$ServiceName-build@$projectId.iam.gserviceaccount.com"

$baseVars = [ordered]@{
    project_id                   = $projectId
    project_number               = $projectNumber
    region                       = $region
    service_name                 = $ServiceName
    repository_id                = $RepositoryId
    allow_unauthenticated_invoke = $allowPublic
    invoker_members              = $invokers
    enable_api_docs              = $enableDocs
    allow_origins                = $allowOrigins
    media_max_mb                 = $mediaMaxMb
    deletion_protection          = $deletionProtection
}

Step 'Initializing Terraform'
Write-VarFile $baseVars
$code = Invoke-Terraform @('init', '-input=false', '-reconfigure', "-backend-config=bucket=$stateBucket", '-backend-config=prefix=sentinel-cloudrun')
if ($code -ne 0) { Fail 'terraform init failed.' }
$code = Invoke-Terraform @('validate')
if ($code -ne 0) { Fail 'terraform validate failed.' }

if ($Destroy) {
    Step 'Destroying the Sentinel deployment'
    Write-Host "This deletes the Cloud Run service, the media bucket and its contents, the Artifact Registry repository and the service accounts in project $projectId." -ForegroundColor Yellow
    $answer = Read-Host "Type the project id ($projectId) to confirm"
    if ($answer -ne $projectId) { Fail 'Confirmation did not match. Nothing was destroyed.' }
    $destroyVars = [ordered]@{}
    foreach ($key in $baseVars.Keys) { $destroyVars[$key] = $baseVars[$key] }
    $destroyVars['deletion_protection'] = $false
    $destroyVars['bucket_force_destroy'] = $true
    $destroyVars['image'] = "$imageBase/${ServiceName}:destroy"
    Write-VarFile $destroyVars
    $code = Invoke-Terraform @('apply', '-input=false', '-auto-approve', "-var-file=$VarFile", '-target=google_cloud_run_v2_service.sentinel', '-target=google_storage_bucket.media')
    if ($code -ne 0) { Fail 'Could not lift deletion protection.' }
    $code = Invoke-Terraform @('destroy', '-input=false', '-auto-approve', "-var-file=$VarFile")
    if ($code -ne 0) { Fail 'terraform destroy failed.' }
    Write-Host 'Destroyed. The state bucket and the Cloud Build source bucket remain.' -ForegroundColor Green
    exit 0
}

Step 'Phase 1: APIs, Artifact Registry and build identity'
$phaseOne = @('apply', '-input=false', '-auto-approve', "-var-file=$VarFile",
    '-target=google_project_service.apis',
    '-target=google_artifact_registry_repository.sentinel',
    '-target=google_service_account.build',
    '-target=google_project_iam_member.build_roles')
if ($PlanOnly) { $phaseOne = @('plan', '-input=false', "-var-file=$VarFile", '-target=google_project_service.apis', '-target=google_artifact_registry_repository.sentinel', '-target=google_service_account.build', '-target=google_project_iam_member.build_roles') }
Invoke-WithRetry { Invoke-Terraform $phaseOne } 'Phase 1'

if ($Image) {
    $imageRef = $Image
} elseif ($PlanOnly) {
    $imageRef = "$imageBase/${ServiceName}:plan-only"
} elseif ($SkipBuild) {
    Step 'Reusing the currently deployed image'
    $imageRef = & gcloud run services describe $ServiceName --project $projectId --region $region --format='value(spec.template.spec.containers[0].image)' 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $imageRef) { Fail 'No deployed image found. Run without -SkipBuild first.' }
    $imageRef = ($imageRef | Select-Object -First 1).Trim()
} else {
    Step 'Building the image with Cloud Build'
    $tag = Get-Date -Format 'yyyyMMdd-HHmmss'
    $imageRef = "$imageBase/${ServiceName}:$tag"
    Invoke-WithRetry {
        & gcloud builds submit $Root --project $projectId --config (Join-Path $PSScriptRoot 'cloudbuild.yaml') --substitutions "_IMAGE=$imageRef" --service-account "projects/$projectId/serviceAccounts/$buildServiceAccount" | Out-Host
        return $LASTEXITCODE
    } 'Cloud Build'
}
Write-Host "Image: $imageRef"

Step 'Phase 2: planning the full deployment'
$fullVars = [ordered]@{}
foreach ($key in $baseVars.Keys) { $fullVars[$key] = $baseVars[$key] }
$fullVars['image'] = $imageRef
Write-VarFile $fullVars
$code = Invoke-Terraform @('plan', '-input=false', "-var-file=$VarFile", "-out=$PlanFile")
if ($code -ne 0) { Fail 'terraform plan failed.' }

if ($PlanOnly) {
    Write-Host 'Plan only: nothing was applied.'
    exit 0
}

Step 'Phase 2: applying'
Invoke-WithRetry { Invoke-Terraform @('apply', '-input=false', $PlanFile) } 'terraform apply'

Step 'Reading outputs'
$serviceUrl = (& $script:Terraform "-chdir=$TfDir" output -raw service_url).Trim()
$expectedUrl = (& $script:Terraform "-chdir=$TfDir" output -raw expected_app_url).Trim()
$docsUrl = "$serviceUrl/docs"
$cardUrl = "$serviceUrl/a2a/app/.well-known/agent-card.json"

Step 'Smoke test'
$headers = @{}
if (-not $allowPublic) {
    $identity = & gcloud auth print-identity-token 2>$null
    if ($LASTEXITCODE -eq 0 -and $identity) { $headers['Authorization'] = "Bearer $(($identity | Select-Object -First 1))" }
}
$healthy = $false
for ($attempt = 1; $attempt -le 8; $attempt++) {
    $response = Invoke-Probe "$serviceUrl/health" $headers
    if ($response -and $response.StatusCode -eq 200) { $healthy = $true; break }
    Start-Sleep -Seconds 10
}
if (-not $healthy) { Fail "Health check failed at $serviceUrl/health. Check Cloud Run logs for service $ServiceName." }
Write-Host "health: OK"

$cardResponse = Invoke-Probe $cardUrl $headers
if (-not $cardResponse -or $cardResponse.StatusCode -ne 200) { Fail "Agent card not reachable at $cardUrl." }
$card = $cardResponse.Content | ConvertFrom-Json
$advertised = $card.url
if ($advertised -ne "$expectedUrl/a2a/app") {
    Fail "Agent card advertises '$advertised' but expected '$expectedUrl/a2a/app'. Set extra_env APP_URL or check the service URL."
}
Write-Host "agent card: OK ($advertised)"

if ($enableDocs) {
    $docsResponse = Invoke-Probe $docsUrl $headers
    if ($docsResponse -and $docsResponse.StatusCode -eq 200) { Write-Host 'docs: OK' } else { Write-Host 'WARNING: /docs did not return 200.' -ForegroundColor Yellow }
}

Write-Host ""
Write-Host 'Deployment complete.' -ForegroundColor Green
Write-Host "Service URL : $serviceUrl"
Write-Host "Swagger docs: $docsUrl"
Write-Host "Agent card  : $cardUrl"
Write-Host "Image       : $imageRef"
if (-not $allowPublic) {
    Write-Host ""
    Write-Host 'The service is IAM-protected. To call it from this machine:'
    Write-Host "  gcloud run services proxy $ServiceName --project $projectId --region $region --port 8080"
    Write-Host '  then use http://localhost:8080 as base_url in Postman.'
}
Write-Host ""
Write-Host 'Postman collection: deploy\postman. Details: deploy\README.md'
