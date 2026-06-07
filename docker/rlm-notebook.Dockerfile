# Runtime image for the RLM "notebook" execution mode (rlm.execution: notebook).
#
# The worker spawns this image as a locked-down sandbox
# (`docker run --network=none --read-only ...`) to execute the recursive review
# notebook. The notebook itself is stdlib-only and talks to the host model
# broker over a bind-mounted Unix socket, so this image deliberately does NOT
# contain franktheunicorn or any API keys — only Jupyter to execute cells and
# ripgrep to back the notebook's ripgrep() search helper.
#
# Build:   docker build -f docker/rlm-notebook.Dockerfile -t frank-rlm-notebook:latest .
# Use:     set `rlm.image: frank-rlm-notebook:latest` in your backend config.
#
# Pinned, minimal, no network at run time.
FROM python:3.12-slim

# ripgrep powers the notebook's ripgrep() helper over the read-only repo mount.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ripgrep \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Just enough to run `jupyter execute notebook.ipynb`.
RUN pip install --no-cache-dir \
    "nbclient>=0.10,<1.0" \
    "ipykernel>=6.29,<7.0" \
    "nbformat>=5.10,<6.0"

# The worker mounts the working dir at /rlm and runs the notebook there.
WORKDIR /rlm
CMD ["jupyter", "execute", "/rlm/review.ipynb"]
