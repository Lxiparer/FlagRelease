# Vendor (baseline) container launch. The runner rewrites --name (auto-derived
# from release_name) and the image; edit the -v mount + GPU flags for your host.
docker run -d --name vendor_container \
  --gpus all \
  --ipc=host --shm-size=32g \
  -p 9181:9181 \
  -v /path/to/models:/path/to/models \
  nvcr.io/nvidia/pytorch:24.01-py3 sleep infinity
