# AI Disclosure

## Tools used

Claude (Anthropic).

## How I used it

**1. Template code for the app and config files**

I used Claude to write a starting version of `app.py` (the Streamlit app with the Prometheus metrics) and the
config files `prometheus.yml`, `rules.yml` and `alertmanager.yml`. The configs follow the samples in the
`Monitoring/` folder of the course repo.

**2. Debugging**

Most of the help I needed was here:
- **Alertmanager inside the VM.** It needs `--cluster.listen-address=` to turn off clustering, which isn't
  needed for a single machine and can stop it from starting in a VM.
- **Prometheus UI pages returning 404.** The Ubuntu build of Prometheus only ships the classic web UI, so
  the pages are under `/classic/` (for example `/classic/targets`).
- **Huge torch download.** A plain `pip install torch` can pull the much bigger GPU build which fails to download in my VM. I added
  `--extra-index-url https://download.pytorch.org/whl/cpu` to `requirements.txt` so the CPU-only build is
  installed.

**3. Formatting the Markdown files**

I used it to format and tidy up `README.md` and this file so they're readable.

## Conclusion

Claude gave me a working starting point and saved a lot of time on VM and setup issues. I ran everything
myself, tested single and bulk uploads, broken files and the alert pipeline, and made my own changes to the
dashboard in Grafana.
