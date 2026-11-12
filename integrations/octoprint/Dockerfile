FROM octoprint/octoprint:latest

# Build an offline wheelhouse. The upstream image can change Python packages
# during container initialization, so installation must also run at startup.
ARG REMOTE_CONNECTION_PLUGIN_URL=https://github.com/mstarostik/OctoPrint-Remote_connection/archive/main.zip
ARG THEMEIFY_PLUGIN_URL=https://github.com/birkbjo/OctoPrint-Themeify/archive/master.zip
ARG PRINT_JOB_HISTORY_PLUGIN_URL=https://github.com/vojtakaniok/OctoPrint-PrintJobHistory/releases/download/1.17.5/main.zip
ARG PREHEAT_PLUGIN_URL=https://github.com/marian42/octoprint-preheat/archive/refs/tags/0.9.0.zip
ARG SPOOL_MANAGER_PLUGIN_URL=https://github.com/WildRikku/OctoPrint-SpoolManager/releases/download/1.7.8/main.zip
ARG FILAMENT_MANAGER_PLUGIN_URL=https://github.com/OllisGit/OctoPrint-FilamentManager/releases/download/1.9.1/master.zip
ARG LAYER_PROGRESS_PLUGIN_URL=https://github.com/OllisGit/OctoPrint-DisplayLayerProgress/releases/download/1.28.0/master.zip
ARG CURA_THUMBNAILS_PLUGIN_URL=https://github.com/jneilliii/OctoPrint-UltimakerFormatPackage/archive/refs/tags/1.2.0.zip
ARG SLICER_THUMBNAILS_PLUGIN_URL=https://github.com/jneilliii/OctoPrint-PrusaSlicerThumbnails/archive/refs/tags/1.2.3.zip
ARG COST_ESTIMATION_PLUGIN_URL=https://github.com/Hillshum/OctoPrint-CostEstimation/releases/download/v3.6.0/master.zip

RUN set -eux; \
    mkdir -p /opt/octoprint-plugin-wheels; \
    python3 -m pip wheel --wheel-dir /opt/octoprint-plugin-wheels \
      "$REMOTE_CONNECTION_PLUGIN_URL" "$THEMEIFY_PLUGIN_URL" "$PRINT_JOB_HISTORY_PLUGIN_URL" \
      "$PREHEAT_PLUGIN_URL" "$SPOOL_MANAGER_PLUGIN_URL" "$FILAMENT_MANAGER_PLUGIN_URL" \
      "$LAYER_PROGRESS_PLUGIN_URL" "$CURA_THUMBNAILS_PLUGIN_URL" \
      "$SLICER_THUMBNAILS_PLUGIN_URL" "$COST_ESTIMATION_PLUGIN_URL"

COPY 90-install-octoprint-plugins /etc/cont-init.d/90-install-octoprint-plugins
RUN chmod 755 /etc/cont-init.d/90-install-octoprint-plugins

# Image-bundled source copied into persistent OctoPrint plugin directory at startup.
COPY neptune_terminal.py /opt/neptune-plugins/neptune_terminal.py

COPY 91-configure-mks-remote /etc/cont-init.d/91-configure-mks-remote
RUN chmod 755 /etc/cont-init.d/91-configure-mks-remote
