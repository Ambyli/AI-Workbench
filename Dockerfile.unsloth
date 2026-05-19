FROM unsloth/unsloth:latest

# Switch to root to allow package installation and filesystem changes
USER root

# Install build dependencies needed to compile llama.cpp from source
RUN apt-get update && apt-get install -y cmake git libcurl4-openssl-dev && rm -rf /var/lib/apt/lists/*

# Copy our custom entrypoint script that builds llama.cpp with CUDA at runtime
COPY entrypoint.sh /entrypoint.sh

# Make the entrypoint script executable
RUN chmod +x /entrypoint.sh

# Switch back to the unsloth user for runtime
USER unsloth

# Override entrypoint with ours, which builds llama.cpp then calls the original
ENTRYPOINT ["/entrypoint.sh"]