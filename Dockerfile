# ============================================================
# OpenClaw Agent - AI Development Agent with Claude (Anthropic)
# Multi-stage build for optimized image size
# ============================================================

# Stage 1: Base image with Python and system dependencies
FROM python:3.12-slim-bookworm AS base

# Prevent Python from writing bytecode and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    wget \
    git \
    zip \
    unzip \
    ca-certificates \
    gnupg \
    apt-transport-https \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# ============================================================
# Stage 2: Install .NET SDK
# ============================================================
FROM base AS dotnet-installer

# Install .NET 9.0 SDK
RUN wget https://dot.net/v1/dotnet-install.sh -O dotnet-install.sh \
    && chmod +x dotnet-install.sh \
    && ./dotnet-install.sh --channel 9.0 --install-dir /usr/share/dotnet \
    && rm dotnet-install.sh

# ============================================================
# Stage 3: Final production image
# ============================================================
FROM python:3.12-slim-bookworm AS final

# Labels for container metadata
LABEL maintainer="ahmanamjardir" \
      version="1.0" \
      description="OpenClaw AI Agent with Claude (Anthropic), .NET SDK, Python, and Node.js"

# Environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DOTNET_ROOT=/usr/share/dotnet \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1 \
    PATH="/usr/share/dotnet:/opt/venv/bin:$PATH" \
    WORKSPACE_DIR=/workspace \
    PORT=8080 \
    CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER_PATH=/usr/bin/chromedriver

# Install runtime dependencies + Chromium for Selenium
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    wget \
    git \
    zip \
    unzip \
    ca-certificates \
    libicu72 \
    libssl3 \
    chromium \
    chromium-driver \
    fonts-liberation \
    libnss3 \
    libxss1 \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Copy .NET SDK from installer stage
COPY --from=dotnet-installer /usr/share/dotnet /usr/share/dotnet

# Install Node.js 20 LTS (minimal install)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g npm@latest \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Create non-root user for security
RUN groupadd --gid 1000 agentuser \
    && useradd --uid 1000 --gid agentuser --shell /bin/bash --create-home agentuser

# Create workspace and app directories
RUN mkdir -p /workspace /app \
    && chown -R agentuser:agentuser /workspace /app

# Set working directory
WORKDIR /app

# Create Python virtual environment
RUN python -m venv /opt/venv \
    && chown -R agentuser:agentuser /opt/venv

# Copy requirements first for better caching
COPY requirements.txt /app/requirements.txt

# Install Python dependencies
RUN /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt \
    && /opt/venv/bin/pip install \
        pytest \
        black \
        pylint \
        mypy \
        httpx

# Copy agent application code
COPY src/ /app/src/

# Set ownership
RUN chown -R agentuser:agentuser /app

# Switch to non-root user
USER agentuser

# Verify installations
RUN dotnet --version \
    && node --version \
    && python --version \
    && chromium --version \
    && /opt/venv/bin/pip list

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Start the agent server with gunicorn
# --chdir /app/src sets the working directory so that 'core' and 'utils'
# are resolvable as top-level packages, and 'app:app' is the entry point.
CMD ["/opt/venv/bin/gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", "--timeout", "600", "--chdir", "/app/src", "app:app"]
