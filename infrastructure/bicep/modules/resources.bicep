param location string
param environment string

// 1. Control Plane Azure SQL Database
resource sqlServer 'Microsoft.Sql/servers@2021-11-01' = {
  name: 'sql-wealth-controlplane-${environment}'
  location: location
  properties: {
    administratorLogin: 'migration_admin'
    administratorLoginPassword: 'ComplexPassword123!'
  }
}

resource sqlDatabase 'Microsoft.Sql/servers/databases@2021-11-01' = {
  parent: sqlServer
  name: 'wealth_migration_control_plane'
  location: location
  sku: {
    name: 'S0'
    tier: 'Standard'
  }
}

// 2. Azure Data Factory Instance
resource adf 'Microsoft.DataFactory/factories@2018-06-01' = {
  name: 'adf-wealth-migration-${environment}'
  location: location
  identity: {
    type: 'SystemAssigned'
  }
}

// 3. Azure Application Insights / Telemetry
resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-wealth-migration-${environment}'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
  }
}

// 4. Control Plane App Service Container
resource appServicePlan 'Microsoft.Web/serverfarms@2021-03-01' = {
  name: 'asp-wealth-migration-${environment}'
  location: location
  sku: {
    name: 'B1'
    tier: 'Basic'
  }
  properties: {
    reserved: true
  }
}

resource controlPlaneApp 'Microsoft.Web/sites@2021-03-01' = {
  name: 'app-wealth-controlplane-${environment}'
  location: location
  properties: {
    serverFarmId: appServicePlan.id
    siteConfig: {
      linuxFxVersion: 'DOCKER|mcr.microsoft.com/appsvc/staticsite:latest'
      appSettings: [
        { name: 'AZURE_RESOURCE_GROUP', value: 'rg-wealth-migration-${environment}' }
        { name: 'ADF_FACTORY_NAME', value: adf.name }
        { name: 'APPINSIGHTS_INSTRUMENTATIONKEY', value: appInsights.properties.InstrumentationKey }
      ]
    }
  }
}

output controlPlaneAppUrl string = controlPlaneApp.properties.defaultHostName
output adfName string = adf.name