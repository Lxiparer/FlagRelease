# Gems setup, sourced inside the container before vLLM launches.
# USE_FLAGGEMS=1 switches vLLM onto the FlagGems operator library.
export USE_FLAGGEMS=1
export OMP_NUM_THREADS=1
export VLLM_ENGINE_READY_TIMEOUT_S=6000
# If the base image does not already ship FlagGems, uncomment:
# pip install flaggems --quiet
