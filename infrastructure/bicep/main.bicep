targetScope = 'subscription'

param location string = 'eastus'
param environment string = 'dev'
param resourceGroupName string = 'rg-wealth-migration-${environment}'

resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: resourceGroupName
  location: location
}

module infrastructure 'modules/resources.bicep' = {
  scope: resourceGroup(rg.name)
  name: 'wealthMigrationInfrastructure'
  params: {
    location: location
    environment: environment
  }
}

output controlPlaneUrl string = infrastructure.outputs.controlPlaneAppUrl
output adfName string = infrastructure.outputs.adfName