## setup

```shell
# use a virt env
python3 -m venv venv
source venv/bin/activate

# install dependencies
# pip install fastapi uvicorn google-genai
# pip freeze > requirements.txt

# export API key if running locally (for AI / Gemini calls)
export GOOGLE_API_KEY=<...>


# run the app
uvicorn main:app --reload
# http://127.0.0.1:8000/

# exit
deactivate

# /Users/jimangel/mql2promql/fastapi
docker build -t mql-converter . 
docker run -p 8000:8000 -e GOOGLE_API_KEY=${GOOGLE_API_KEY} mql-converter
```

```
gcloud auth login
gcloud config set project mql-cloudrun

gcloud artifacts repositories create mvp \
    --repository-format=docker \
    --location=us-south1 \
    --description="Docker container repository"

docker buildx create --name multiplatform-builder --driver docker-container --use


docker buildx build --platform linux/amd64,linux/arm64 \
    -t us-south1-docker.pkg.dev/mql-cloudrun/mvp/mql-converter:latest \
    --push .

docker run -p 8080:8080 -e GOOGLE_API_KEY=${GOOGLE_API_KEY} -e UVICORN_PORT=8080 us-south1-docker.pkg.dev/mql-cloudrun/mvp/mql-converter

# allow default service account to read
gcloud projects add-iam-policy-binding mql-cloudrun \
    --member="serviceAccount:$(gcloud projects describe mql-cloudrun --format='value(projectNumber)')-compute@developer.gserviceaccount.com" \
    --role="roles/artifactregistry.reader"

# using port 8080 as set by cloudrun.
gcloud run deploy mql2prom-conv-service \
    --project="$(gcloud projects describe mql-cloudrun --format='value(projectId)')" \
    --image=us-south1-docker.pkg.dev/mql-cloudrun/mvp/mql-converter:latest \
    --platform=managed \
    --region=us-south1 \
    --allow-unauthenticated \
    --set-env-vars=GOOGLE_API_KEY=${GOOGLE_API_KEY},UVICORN_PORT=8080
```