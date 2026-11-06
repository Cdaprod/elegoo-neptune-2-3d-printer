FROM octoprint/octoprint:latest

# Install into OctoPrint's own virtual environment, not the system Python.
# Fail the build if the environment or plugin cannot be installed.
ARG REMOTE_CONNECTION_PLUGIN_URL=https://github.com/mstarostik/OctoPrint-Remote_connection/archive/main.zip
RUN set -eux; \
    test -x /opt/octoprint/venv/bin/python; \
    /opt/octoprint/venv/bin/python -m pip install --no-cache-dir "$REMOTE_CONNECTION_PLUGIN_URL"
