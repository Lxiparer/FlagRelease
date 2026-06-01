# Gems-side container launch. The runner rewrites --name (auto-derived from
# release_name) and the image; edit the -v mount + GPU flags for your host.
# Mount the host dir that contains your model so model_path is visible inside.
docker run -d --name gems_container \
  --gpus all \
  --ipc=host --shm-size=32g \
  -p 9181:9181 \
  -v /path/to/models:/path/to/models \
  flaggems/vllm:latest sleep infinity
