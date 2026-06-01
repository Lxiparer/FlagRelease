vllm serve /path/to/models/your-model \
  --served-model-name "model" \
  --host 0.0.0.0 \
  --port 9181 \
  --tensor-parallel-size 1 \
  --max-model-len 4096
