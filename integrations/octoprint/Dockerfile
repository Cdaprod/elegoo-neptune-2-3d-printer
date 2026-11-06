FROM octoprint/octoprint:latest

# Build an offline wheelhouse. The upstream image can change Python packages
# during container initialization, so installation must also run at startup.
ARG REMOTE_CONNECTION_PLUGIN_URL=https://github.com/mstarostik/OctoPrint-Remote_connection/archive/main.zip
ARG THEMEIFY_PLUGIN_URL=https://github.com/birkbjo/OctoPrint-Themeify/archive/master.zip

RUN set -eux; \
    mkdir -p /opt/octoprint-plugin-wheels; \
    python3 -m pip wheel --wheel-dir /opt/octoprint-plugin-wheels \
      "$REMOTE_CONNECTION_PLUGIN_URL" "$THEMEIFY_PLUGIN_URL"

COPY 90-install-octoprint-plugins /etc/cont-init.d/90-install-octoprint-plugins
RUN chmod 755 /etc/cont-init.d/90-install-octoprint-plugins
