# =============================================================================
# Eco-Travel Advisor — single-container image
# =============================================================================
# Built to deploy on HuggingFace Spaces (Docker SDK) in one click: Spaces
# builds this Dockerfile and routes all external traffic to ONE port
# (see EXPOSE / README.md's `app_port`). Internally, start.sh runs all
# four processes (data_services, Rasa action server, Rasa server,
# Streamlit dashboard) and only Streamlit is reachable from outside — the
# other three talk to each other over localhost inside the same container.
#
# For local development where you want the four pieces properly isolated
# (closer to a real production topology), use docker-compose.yml instead,
# which builds this SAME image but runs each service in its own container.
# =============================================================================

FROM python:3.10-slim

# python:3.10-slim is not a stylistic choice — rasa==3.6.21 (see
# requirements.txt) requires Python >=3.8,<3.11. 3.11/3.12 will not install it.

# curl: used by start.sh to wait for each service to be ready before
# starting the next one. build-essential: a couple of Rasa's dependencies
# need a C compiler if no prebuilt wheel matches this exact platform.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so `docker build` can cache this (slow) layer across
# rebuilds that only touch the bot's own code/data below.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x start.sh

# Fails the BUILD (loudly, in the Spaces build log) rather than the
# container (silently, at 3am) if domain.yml/nlu.yml/rules.yml/stories.yml
# reference something that doesn't exist — this is `rasa data validate`,
# the exact command Worksheet 9 ("Making a Bot Behave" §6.4) says to run
# before every `rasa train`.
RUN rasa data validate

# Baking the trained model into the image means the container starts in
# seconds instead of training from scratch every time a free-tier Space
# wakes up from sleep.
RUN rasa train --fixed-model-name eco-travel-advisor

# HuggingFace Spaces (Docker SDK) routes external traffic to exactly ONE
# port. This must match `app_port` in README.md's YAML frontmatter.
ENV PORT=7860
EXPOSE 7860

CMD ["./start.sh"]
