# Set your variables
REGION="us-south1"  # From the Cloud Run service location
CLOUD_RUN_SERVICE="mql2prom-conv-service"
PROJECT_ID="623927437995"  # From the namespace
LB_NAME="mql-converter-lb"
NETWORK_NAME="mql-network"
SUBNET_NAME="mql-subnet"

# 2. Create VPC network and subnet
gcloud compute networks create $NETWORK_NAME --subnet-mode=custom

gcloud compute networks subnets create $SUBNET_NAME \
  --network=$NETWORK_NAME \
  --range=10.1.2.0/24 \
  --region=$REGION

# 3. Create proxy-only subnet
gcloud compute networks subnets create proxy-only-subnet \
  --purpose=REGIONAL_MANAGED_PROXY \
  --role=ACTIVE \
  --region=$REGION \
  --network=$NETWORK_NAME \
  --range=10.129.0.0/23

# 4. Create serverless NEG
gcloud compute network-endpoint-groups create mql-serverless-neg \
  --region=$REGION \
  --network-endpoint-type=serverless \
  --cloud-run-service=$CLOUD_RUN_SERVICE

# 5. Create backend service
gcloud compute backend-services create mql-backend-service \
  --load-balancing-scheme=EXTERNAL_MANAGED \
  --protocol=HTTP \
  --region=$REGION

# 6. Add serverless NEG to backend service
gcloud compute backend-services add-backend mql-backend-service \
  --network-endpoint-group=mql-serverless-neg \
  --network-endpoint-group-region=$REGION \
  --region=$REGION

# 7. Create URL map
gcloud compute url-maps create $LB_NAME \
  --region=$REGION \
  --default-service=mql-backend-service

# 8. Create target HTTP proxy
gcloud compute target-http-proxies create $LB_NAME-proxy \
  --region=$REGION \
  --url-map=$LB_NAME

# 9. Create forwarding rule (the actual load balancer frontend)
gcloud compute forwarding-rules create $LB_NAME-forwarding-rule \
  --region=$REGION \
  --load-balancing-scheme=EXTERNAL_MANAGED \
  --network=$NETWORK_NAME \
  --subnet=$SUBNET_NAME \
  --ports=80 \
  --target-http-proxy=$LB_NAME-proxy

# 10. Get the load balancer IP address
gcloud compute forwarding-rules describe $LB_NAME-forwarding-rule \
  --region=$REGION \
  --format="get(IPAddress)"