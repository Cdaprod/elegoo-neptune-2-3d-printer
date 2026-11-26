# Why my Dockerized OctoPrint plugins disappeared — and the startup fix

> Publication draft · Cdaprod · November 2026  
> Status: **Engineering write-up; physical startup verification pending.** Do not present the mock test as proof of live hardware compatibility.

## Hook

My OctoPrint container built successfully. But when I checked its runtime plugin list, the plugins I expected were missing. A successful Docker build is not the same as a verified application startup.

The Neptune 2 integration bundles [Remote Connection](https://github.com/mstarostik/OctoPrint-Remote_connection) and [Themeify](https://github.com/birkbjo/OctoPrint-Themeify). The original setup built plugin wheels into the image, but the upstream OctoPrint initialization process could leave the final Python environment without them.

## What changed

The Dockerfile builds an offline wheelhouse at `/opt/octoprint-plugin-wheels` during image construction. The s6 initializer then uses the absolute interpreter path `/usr/local/bin/python3` and runs `pip install --no-index --find-links=...` at container startup.

That last detail matters: s6 initialization can have a restricted `PATH`. An unqualified `python3` command may not resolve as expected, even when the binary exists in the image.

After installation, the initializer checks `importlib.metadata.entry_points(group="octoprint.plugin")` for **both** `remote_connection` and `themeify`. If either is absent, startup fails with an explicit error rather than silently declaring success.

## What the tests prove — and what they do not

The hardware-free regression test exercises the shell initializer with a temporary fake Python executable. It checks the absolute interpreter path, offline wheel-install arguments, plugin names, entry-point enforcement, and failure when the interpreter is missing.

**This is not** a live OctoPrint image-build verification, a proof that the plugins load in the production OctoPrint environment, or a test of MKS WiFi compatibility. Those remain explicit commissioning checks.

## Reproduce safely

From a repository checkout, run the simulator without connecting to the printer:

```sh
python3 -m unittest discover -s tests -p 'test_octoprint_startup.py' -v
```

For later image verification in a controlled environment, build without starting a print, inspect the `[neptune-plugins]` startup log lines, and verify both plugin entry points in the running container. Preserve the `octoprint-data` volume; do not use `docker compose down -v` as a routine reset.

## The reusable DevOps lesson

Images are build artifacts. Running applications are states. When a base image performs startup initialization, verify what actually exists **after** that initialization, not just what your Dockerfile installed earlier.

- Repo: https://github.com/Cdaprod/elegoo-neptune-2-3d-printer
- Startup fix: https://github.com/Cdaprod/elegoo-neptune-2-3d-printer/commit/2335a5d8250a91e91428ddd17d93d2b0be86b233
- Hardware-free test: https://github.com/Cdaprod/elegoo-neptune-2-3d-printer/pull/1

## 60-second short storyboard

**0–7 s:** “My OctoPrint image built perfectly — but my plugins were missing at runtime.”

**7–20 s:** Show the wheelhouse in the Dockerfile and why build-time installation was insufficient.

**20–40 s:** Highlight the absolute `/usr/local/bin/python3` path, `--no-index`, and `octoprint.plugin` entry-point check.

**40–52 s:** Run the no-hardware unit test. Overlay: “Simulated initializer — not live printer validation.”

**52–60 s:** “The fix is in my GitHub. What startup-state bug caught you?” Display the repository URL.

**CTA:** Open an issue with reproduction details, contribute a startup check, or follow the Neptune Observatory work.
