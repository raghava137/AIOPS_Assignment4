# DA3408 Assignment 4: Monitoring with Prometheus and Grafana

For this assignment I built a small Streamlit app that writes captions for images using the BLIP model
(`Salesforce/blip-image-captioning-base`). It runs only on CPU. You can upload a single image, or a zip
file with many images and get a CSV of captions back.

The app exposes its metrics on port 18000. Prometheus scrapes the app and node_exporter (which gives the
machine's CPU, RAM, disk and network numbers). Alerts are sent to Alertmanager, and Grafana shows
everything on one dashboard.

How they are:

```
streamlit app (port 8501, metrics on 18000) --+
                                              +--> prometheus (9090) --> grafana (3000)
node_exporter (9100) -------------------------+         |
                                                        +--> alertmanager (9093) --> webhook.site
```

I ran everything on an Ubuntu VM in UTM.


## Files in this repo

- `app.py` - the Streamlit app. All the Prometheus metrics are defined at the top of this file.
- `requirements.txt` - python packages. It points pip to the CPU-only torch build so it doesn't download the huge GPU version.
- `prometheus.yml` - tells Prometheus what to scrape (the app, node_exporter, itself) and how often (every 2s).
- `rules.yml` - the alert rules (app down, slow captions, too many errors, high CPU).
- `alertmanager.yml` - where alerts get sent. I used a webhook.site URL. It's based on the sample in the course repo.
- `dashboard.json` - the Grafana dashboard export.


## Setup

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip prometheus prometheus-node-exporter prometheus-alertmanager

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Grafana is not in Ubuntu's default repo, so I added Grafana's repo first:

```bash
sudo mkdir -p /etc/apt/keyrings
wget -q -O - https://apt.grafana.com/gpg.key | gpg --dearmor | sudo tee /etc/apt/keyrings/grafana.gpg > /dev/null
echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" | sudo tee /etc/apt/sources.list.d/grafana.list
sudo apt update && sudo apt install -y grafana
```

Then open https://webhook.site, copy the URL it gives you and paste it into `alertmanager.yml`.


## Running it

apt starts its own Prometheus and Alertmanager in the background using default configs. Those need to be
stopped first, otherwise port 9090 is already taken. Then start Grafana.

Terminal 1:
```bash
sudo systemctl disable --now prometheus prometheus-alertmanager
sudo systemctl start grafana-server
prometheus --config.file=prometheus.yml --storage.tsdb.path=./data
```

Terminal 2:
```bash
prometheus-alertmanager --config.file=alertmanager.yml --cluster.listen-address=
```
(`--cluster.listen-address=` turns off clustering. Without it Alertmanager wouldn't start in my VM.)

Terminal 3:
```bash
source .venv/bin/activate
streamlit run app.py
```

node_exporter is already running as a service after the install, so it doesn't need its own terminal.

Open the app at http://localhost:8501 first. The metrics server only starts once the page is loaded, and
the first time it also downloads the model (around 1 GB).

Other pages:
- http://localhost:18000/metrics - raw metrics from the app
- http://localhost:9090/classic/targets - all 3 targets should be UP (the Ubuntu build of Prometheus only has the classic UI)
- http://localhost:9090/classic/alerts - alerts
- http://localhost:3000 - Grafana (login admin / admin)

To load the dashboard in Grafana: Connections > Data sources > add Prometheus with URL `http://localhost:9090`,
click Save & test. Then Dashboards > New > Import and upload `dashboard.json`.


## Metrics

Most metrics have a `mode` label that is either `single` or `bulk`. The request counter also has
`client_ip` and `session_id`, so I can see which client is sending how many requests.

Counters:
- `caption_requests_total` - uploads, per client ip and session
- `caption_images_processed_total` - images processed, with `status` = success or error
- `caption_errors_total` - errors grouped by the exception name
- `caption_input_bytes_total` - total bytes of images received
- `caption_zip_skipped_total` - files inside a zip that I skipped (not images, or the `__MACOSX` junk files)

Gauges:
- `caption_active_requests` - requests being processed right now
- `caption_bulk_queue` - images still waiting in a zip that is being processed
- `caption_model_loaded` - 1 once the model is loaded
- `caption_memory_bytes`, `caption_cpu_percent` - RAM and CPU used by the app process
- `caption_start_time_seconds` - used to show uptime

Histograms:
- `caption_inference_seconds` - how long the model takes per image
- `caption_image_size_bytes` - size of uploaded images
- `caption_words` - number of words in each caption

Summaries:
- `caption_request_seconds` - total time per upload (gives `_sum` and `_count`)
- `caption_preprocess_seconds` - time to open and resize the image

Info:
- `caption_app_info` - model name, device and torch version


## Some of the PromQL used

Throughput (images per second):
```
sum by (mode) (rate(caption_images_processed_total{status="success"}[30s]))
```
`rate` looks at how much the counter went up in the last 30s and divides by 30. If it went from 120 to
150, that's 30/30 = 1 image per second.

Average time per upload:
```
rate(caption_request_seconds_sum[1m]) / rate(caption_request_seconds_count[1m])
```
Total time spent divided by number of uploads. If 4 uploads took 10s in total, the average is 2.5s.

95th percentile latency:
```
histogram_quantile(0.95, sum by (le) (rate(caption_inference_seconds_bucket[1m])))
```
For example: 20 images in the last minute, and the cumulative bucket counts are le=1: 8, le=1.5: 15,
le=2: 19, le=3: 20. The 95th percentile is image number 0.95 x 20 = 19. That falls in the 1.5 to 2 bucket
(counts 15 to 19), so p95 = 1.5 + (19 - 15) / (19 - 15) x 0.5 = 2.0 seconds.

System CPU %:
```
100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[30s])))
```
node_exporter counts how many seconds each core spent idle. If the cores were idle 25% of the time, the
CPU was 75% busy.

System RAM %:
```
100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)
```


## Dashboard

The dashboard goes from top to bottom:
1. Health - is the app up, is node_exporter up, any alerts firing
2. Traffic and latency - throughput, p50/p95/p99, time per upload, errors
3. Clients - uploads per IP and per session, current queue
4. App resources - memory and CPU of the app
5. System - CPU, RAM, disk, network from node_exporter
6. App vs system - system CPU plotted with bulk images/sec on the same time axis, to show the CPU spikes come from bulk uploads

For the plotting rules from class:
- colour-blind safe colours (Okabe-Ito palette), and the up/down status is shown as text as well as colour
- every line has point markers and a different line style (solid, dashed, dotted), so it still works when printed in black and white
- every axis is labelled with the unit and the scale (linear or log)
- legends on every panel that has more than one line
- each panel has a title and a short subtitle, and a description you can see by hovering the (i)


## Alerts

Defined in `rules.yml`:
- `AppDown` - Prometheus can't scrape the app for 10s
- `SlowCaptions` - p95 caption time above 5s for 30s
- `ManyErrors` - more than 10% of images failing
- `HighCPU` - system CPU above 85% for 30s

To test: stop the app with Ctrl+C. After about 10 seconds `AppDown` shows up on the alerts page and a
request arrives on the webhook.site page.
