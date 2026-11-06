FROM octoprint/octoprint:latest

# The upstream OctoPrint Docker image uses the system Python installation.
# Install the plugin into the same interpreter OctoPrint runs under.
ARG REMOTE_CONNECTION_PLUGIN_URL=https://github.com/mstarostik/OctoPrint-Remote_connection/archive/main.zip
ARG THEMEIFY_PLUGIN_URL=https://github.com/birkbjo/OctoPrint-Themeify/archive/master.zip
RUN set -eux; \
    python3 -m pip install --no-cache-dir "$REMOTE_CONNECTION_PLUGIN_URL" "$THEMEIFY_PLUGIN_URL"
