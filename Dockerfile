FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NODE_VERSION=20

WORKDIR /app

# Install system deps: ffmpeg, Node.js, git
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    ffmpeg \
    git \
    curl \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_VERSION}.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt ./
RUN python -m pip install --upgrade pip && pip install -r requirements.txt

# Clone and build bgutil-ytdlp-pot-provider server
RUN git clone --single-branch --branch 1.2.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil \
    && cd /opt/bgutil/server \
    && npm install \
    && npx tsc

COPY . .

EXPOSE 8080 4416

# Create startup script that runs bgutil server in background, then starts bot
RUN echo '#!/bin/bash\n\
set -e\n\
echo "Starting bgutil PO token provider..."\n\
cd /opt/bgutil/server && node build/main.js &\n\
sleep 2\n\
echo "Starting Telegram bot..."\n\
cd /app\n\
exec python -m app.bot' > /app/start.sh \
    && chmod +x /app/start.sh

CMD ["/app/start.sh"]
