# Project Setup Guide

This guide walks through the complete setup process for deploying the project infrastructure on Azure.

## Prerequisites

- Azure CLI installed and configured
- kubectl installed
- Helm installed
- Valid Azure subscription

## Setup Steps

### 1. Resource Group Creation
```bash
# Create a new resource group
az group create --name <resource-group-name> --location <location>
```

### 2. Azure Data Explorer (ADX) Cluster
```bash
# Create ADX cluster
az kusto cluster create \
  --name <cluster-name> \
  --resource-group <resource-group-name> \
  --location <location> \
  --sku name="Standard_D11_v2" tier="Standard" \
  --enable-disk-encryption true
```

### 3. Database Setup
```bash
# Create database in ADX cluster
az kusto database create \
  --cluster-name <cluster-name> \
  --database-name <database-name> \
  --resource-group <resource-group-name> \
  --soft-delete-period P365D \
  --hot-cache-period P31D
```
Query init_script.kql using Azure Portal->ADX Cluster-> Databases-> Query to initialize the database with Table and Mapping Table.

### 4. Application Registration
1. Navigate to Azure Portal > Azure Active Directory > App registrations
2. Click "New registration"
3. Enter application name and configure supported account types
4. Note down the Application (client) ID and Directory (tenant) ID

### 5. AKS Cluster Setup
```bash
# Create AKS cluster
az aks create \
  --resource-group <resource-group-name> \
  --name <aks-cluster-name> \
  --node-count 3 \
  --enable-managed-identity \
  --generate-ssh-keys
```

### 6. Azure Container Registry (ACR)
```bash
# Create ACR
az acr create \
  --resource-group <resource-group-name> \
  --name <acr-name> \
  --sku Standard

# Connect ACR with AKS
az aks update \
  --name <aks-cluster-name> \
  --resource-group <resource-group-name> \
  --attach-acr <acr-name>
```
# Login to ACR
az acr login --name <acr-name>
```
# Build image to ACR
docker build -t {image-name}:{tag} ./image

# Tag the image for ACR
docker tag {image-name}:{tag} <acr-name>.azurecr.io/{image-name}:{tag}

# Push the image to ACR
docker push <acr-name>.azurecr.io/{image-name}:{tag}

### 7. Key Vault Setup
```bash
# Create Key Vault
az keyvault create \
  --name <keyvault-name> \
  --resource-group <resource-group-name> \
  --location <location>
```
# Get the AKS managed identity ID
IDENTITY_ID=$(az aks show --name weather-app-cluster --resource-group weather-project --query identityProfile.kubeletidentity.clientId -o tsv)

# Grant Key Vault permissions to AKS
az keyvault set-policy --name weather-app-vault --resource-group weather-project --spn 012dbd80-4469-4d3d-b0fe-2ea2cf18f100 --secret-permissions get list
# Store your secrets
az keyvault secret set --vault-name $VAULT_NAME --name "kusto-query-uri" --value "https://${CLUSTER_NAME}.${REGION}.kusto.windows.net"
az keyvault secret set --vault-name $VAULT_NAME --name "kusto-ingest-uri" --value "https://ingest-${CLUSTER_NAME}.${REGION}.kusto.windows.net"
az keyvault secret set --vault-name $VAULT_NAME --name "kusto-db" --value "$DB_NAME"
az keyvault secret set --vault-name $VAULT_NAME --name "app-id" --value "$APP_ID"
az keyvault secret set --vault-name $VAULT_NAME --name "app-key" --value "$APP_KEY"
az keyvault secret set --vault-name $VAULT_NAME --name "tenant-id" --value "$TENANT_ID"


### 8. Helm Setup and Required Charts

```bash
# Add the secrets-store-csi-driver repository
helm repo add secrets-store-csi-driver https://kubernetes-sigs.github.io/secrets-store-csi-driver/charts

# Update helm repositories
helm repo update

# Install secrets-store-csi-driver
helm install csi-secrets-store secrets-store-csi-driver/secrets-store-csi-driver \
  --namespace kube-system \
  --set syncSecret.enabled=true

# Install Azure Key Vault Provider
helm repo add csi-secrets-store-provider-azure https://azure.github.io/secrets-store-csi-driver-provider-azure/charts
helm install azure-csi-provider csi-secrets-store-provider-azure/csi-secrets-store-provider-azure \
  --namespace kube-system
```
### 9. Deploy Security Configuration

Modify the file named `sec-dep.yml` with the following content:

```yaml
apiVersion: secrets-store.csi.x-k8s.io/v1
kind: SecretProviderClass
metadata:
  name: azure-kvname
spec:
  provider: azure
  parameters:
    usePodIdentity: "false"
    useVMManagedIdentity: "true"
    userAssignedIdentityID: {$MANAGED_IDENTITY_ID} #ID of your AKS agentpool
    keyvaultName: {$KEY_VAULT_NAME} #Your keyvault Name
    objects: |
      array:
        - |
          objectName: kusto-query-uri
          objectType: secret
        - |
          objectName: kusto-ingest-uri
          objectType: secret
        - |
          objectName: kusto-db
          objectType: secret
        - |
          objectName: app-id
          objectType: secret
        - |
          objectName: app-key
          objectType: secret
        - |
          objectName: tenant-id
          objectType: secret
    tenantId: {$TENANT_ID$} #Your Tenant ID



### 10. Apply the Configuration

```bash
# Apply the security deployment configuration
kubectl apply -f sec-dep.yml
```

### 11. Access Application

1. Get the ingress controller IP:
```bash
# Get ingress controller external IP
kubectl get ingress #If IP is not present then describe the ingress to get IP 
```

2. Access the application:
```bash

## Troubleshooting

- If permissions are not working, verify the Managed Identity assignments
- Check AKS logs using: `kubectl logs -n <namespace> <pod-name>`
- Verify Key Vault access using: `az keyvault secret list --vault-name <keyvault-name>`
- For Helm-related issues:
  ```bash
  # Check helm releases
  helm list --all-namespaces
  
  # Check CSI driver status
  kubectl get pods -n kube-system -l app=secrets-store-csi-driver
  ```

## Security Notes

- Keep all credentials and secrets in Key Vault
- Regularly rotate access keys and credentials
- Use minimum required permissions for each service principal
- Enable monitoring and logging for all services
- Ensure proper network policies are in place
- Regularly update Helm charts to their latest stable versions
