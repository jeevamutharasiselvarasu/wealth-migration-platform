# Deploys the XODUS platform to Azure Container Apps. Run it yourself from the repository root, signed in with `az login`:
#   powershell -File deploy/azure/deploy_container_app.ps1 -ResourceGroup rg-xodus -Location eastus -Prefix xodus
# The app's settings (secrets included) are read from deploy/azure/app.settings.env (copy app.settings.env.example; never commit it)
# and stored as Container Apps secrets, referenced by the environment variables. Safe to run again: it updates what exists.
#
# What it creates: a container registry (builds the image from Dockerfile.control_plane in Azure), a Container Apps environment and
# one container app with external HTTPS ingress and exactly one replica (sign-in sessions are kept in memory).
param(
    [Parameter(Mandatory = $true)][string]$ResourceGroup,
    [Parameter(Mandatory = $true)][string]$Location,
    [string]$Prefix = "xodus",
    [string]$SettingsFile = "deploy/azure/app.settings.env",
    [string]$Tag = (Get-Date -Format "yyyyMMddHHmmss")
)
$ErrorActionPreference = "Stop"

if (-not (Test-Path $SettingsFile)) { throw "Settings file $SettingsFile not found. Copy deploy/azure/app.settings.env.example and fill it in." }
$settings = [ordered]@{}
foreach ($line in Get-Content $SettingsFile) {
    $t = $line.Trim()
    if (-not $t -or $t.StartsWith("#")) { continue }
    $i = $t.IndexOf("=")
    if ($i -lt 1) { continue }
    $settings[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1).Trim()
}
foreach ($required in @("DATABASE_URL", "AZURE_BLOB_CONNECTION_STRING", "XODUS_BOOTSTRAP_ADMIN_EMAIL")) {
    if (-not $settings[$required]) { throw "$required is required in $SettingsFile." }
}
if (-not $settings["DATABASE_URL"].StartsWith("mssql://")) { throw "DATABASE_URL must point at the XODUS database (mssql://...)." }

$acr = ($Prefix -replace "[^a-z0-9]", "") + "acr"
$envName = "$Prefix-env"
$app = "$Prefix-platform"
$image = "$acr.azurecr.io/xodus-platform:$Tag"

az group create --name $ResourceGroup --location $Location --output none
if (-not (az acr show --name $acr --resource-group $ResourceGroup --query name --output tsv 2>$null)) {
    az acr create --name $acr --resource-group $ResourceGroup --sku Basic --admin-enabled true --output none
}
Write-Host "Building the image in Azure ($image)..."
az acr build --registry $acr --image "xodus-platform:$Tag" --file Dockerfile.control_plane . --output none
$acrUser = az acr credential show --name $acr --query username --output tsv
$acrPass = az acr credential show --name $acr --query "passwords[0].value" --output tsv

if (-not (az containerapp env show --name $envName --resource-group $ResourceGroup --query name --output tsv 2>$null)) {
    az containerapp env create --name $envName --resource-group $ResourceGroup --location $Location --output none
}

# Every setting becomes a secret (names: lower case, dashes) and an environment variable that references it.
$secrets = @(); $envVars = @("PORT=8000", "XODUS_SECURE_COOKIES=1")
foreach ($k in $settings.Keys) {
    if (-not $settings[$k]) { continue }  # Container Apps does not accept an empty secret; an unset setting is simply absent
    $name = ($k.ToLower() -replace "_", "-")
    $secrets += "$name=$($settings[$k])"
    $envVars += "$k=secretref:$name"
}

if (az containerapp show --name $app --resource-group $ResourceGroup --query name --output tsv 2>$null) {
    az containerapp secret set --name $app --resource-group $ResourceGroup --secrets $secrets --output none
    az containerapp update --name $app --resource-group $ResourceGroup --image $image --set-env-vars $envVars --min-replicas 1 --max-replicas 1 --output none
} else {
    az containerapp create --name $app --resource-group $ResourceGroup --environment $envName --image $image `
        --registry-server "$acr.azurecr.io" --registry-username $acrUser --registry-password $acrPass `
        --target-port 8000 --ingress external --min-replicas 1 --max-replicas 1 --cpu 1.0 --memory 2.0Gi `
        --secrets $secrets --env-vars $envVars --output none
}
$fqdn = az containerapp show --name $app --resource-group $ResourceGroup --query properties.configuration.ingress.fqdn --output tsv
$ips = az containerapp show --name $app --resource-group $ResourceGroup --query properties.outboundIpAddresses --output tsv
Write-Host ""
Write-Host "XODUS is deployed: https://$fqdn"
Write-Host "Outbound IP addresses of the app (allow them where a firewall applies):"
Write-Host $ips
Write-Host "Next: allow these addresses on the Azure SQL server (or 'Allow Azure services and resources') and on the storage account's"
Write-Host "network rules, then open https://$fqdn/healthz : it must report database mssql."
