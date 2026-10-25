# Password Reset worker (runs automatically on GitHub Actions)
Do not run anything by hand. The portal starts the `worker` workflow whenever a reset is requested.
Required repository secrets: GAS_URL (web app /exec URL), WORKER_TOKEN.
