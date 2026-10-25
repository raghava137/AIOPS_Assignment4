# DA3408 A4 - image captioning on CPU, monitored with prometheus
# run:  streamlit run app.py      (metrics show up at http://localhost:18000/metrics)

import io
import time
import zipfile

import pandas as pd
import psutil
import streamlit as st
import torch
from PIL import Image
from prometheus_client import (Counter, Gauge, Histogram, Info, Summary,
                               disable_created_metrics, start_http_server)
from streamlit.runtime.scriptrunner import get_script_run_ctx
from transformers import BlipForConditionalGeneration, BlipProcessor

MODEL = "Salesforce/blip-image-captioning-base"
MAX_IMAGES = 100   # max images we caption from one zip
IMG_TYPES = ("jpg", "jpeg", "png", "webp", "bmp")


# streamlit re-runs this file on every click, so the metrics (and the metrics
# server) have to be created only once. cache_resource takes care of that.
@st.cache_resource
def setup_metrics():
    disable_created_metrics()
    start_http_server(18000)
    proc = psutil.Process()

    m = {
        # counters
        "requests": Counter("caption_requests", "uploads, per client",
                            ["mode", "client_ip", "session_id"]),
        "images": Counter("caption_images_processed", "images processed", ["mode", "status"]),
        "errors": Counter("caption_errors", "errors by type", ["mode", "error_type"]),
        "bytes": Counter("caption_input_bytes", "image bytes received", ["mode"]),
        "skipped": Counter("caption_zip_skipped", "files in a zip we didn't caption", ["reason"]),
        # gauges
        "active": Gauge("caption_active_requests", "requests running right now", ["mode"]),
        "queue": Gauge("caption_bulk_queue", "images waiting inside running zip jobs"),
        "loaded": Gauge("caption_model_loaded", "1 once the model is in memory"),
        "memory": Gauge("caption_memory_bytes", "RAM used by the app (RSS)"),
        "cpu": Gauge("caption_cpu_percent", "CPU used by the app, 100 = one core"),
        "started": Gauge("caption_start_time_seconds", "when the app started (unix time)"),
        # histograms
        "latency": Histogram("caption_inference_seconds", "model time per image", ["mode"],
                             buckets=(0.25, 0.5, 1, 1.5, 2, 3, 5, 8, 13)),
        "size": Histogram("caption_image_size_bytes", "uploaded image size", ["mode"],
                          buckets=(5e4, 1e5, 5e5, 1e6, 5e6, 1e7)),
        "words": Histogram("caption_words", "words per caption", ["mode"],
                           buckets=(3, 5, 7, 9, 12, 15)),
        # summaries (these give _sum and _count)
        "request_time": Summary("caption_request_seconds", "total time per upload", ["mode"]),
        "prep_time": Summary("caption_preprocess_seconds", "image decode + resize time", ["mode"]),
    }
    # memory / cpu are read when prometheus scrapes, no extra thread needed
    m["memory"].set_function(lambda: proc.memory_info().rss)
    m["cpu"].set_function(lambda: proc.cpu_percent(None))
    m["started"].set_to_current_time()
    Info("caption_app", "app info").info({"model": MODEL, "device": "cpu",
                                          "torch": torch.__version__})
    return m


M = setup_metrics()


@st.cache_resource(show_spinner="Loading model (first time downloads ~1GB)...")
def load_model():
    processor = BlipProcessor.from_pretrained(MODEL)
    model = BlipForConditionalGeneration.from_pretrained(MODEL).to("cpu").eval()
    M["loaded"].set(1)
    return processor, model


def who_is_calling():
    # ip is None when you open the app on localhost
    ip = st.context.ip_address or "localhost"
    ctx = get_script_run_ctx()
    session = ctx.session_id[:8] if ctx else "unknown"
    return ip, session


def caption(data, mode):
    processor, model = load_model()
    M["bytes"].labels(mode).inc(len(data))
    M["size"].labels(mode).observe(len(data))
    try:
        start = time.time()
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img.thumbnail((1024, 1024))  # big photos are slow and don't help the model
        inputs = processor(images=img, return_tensors="pt")
        M["prep_time"].labels(mode).observe(time.time() - start)

        with M["latency"].labels(mode).time(), torch.no_grad():
            out = model.generate(**inputs, max_new_tokens=30)
        text = processor.decode(out[0], skip_special_tokens=True)
    except Exception as e:
        M["images"].labels(mode, "error").inc()
        M["errors"].labels(mode, type(e).__name__).inc()
        return None

    M["images"].labels(mode, "success").inc()
    M["words"].labels(mode).observe(len(text.split()))
    return text


def images_in_zip(zf):
    names = []
    for f in zf.infolist():
        name = f.filename.split("/")[-1]
        if f.is_dir():
            continue
        if f.filename.startswith("__MACOSX") or name.startswith("."):
            M["skipped"].labels("hidden").inc()   # junk files mac adds when zipping
        elif name.lower().split(".")[-1] not in IMG_TYPES:
            M["skipped"].labels("not_image").inc()
        else:
            names.append(f.filename)
    if len(names) > MAX_IMAGES:
        M["skipped"].labels("over_limit").inc(len(names) - MAX_IMAGES)
        st.warning(f"Only the first {MAX_IMAGES} images will be captioned.")
    return sorted(names)[:MAX_IMAGES]


st.set_page_config(page_title="Image Captioning")
st.title("Image Captioning (CPU)")
st.caption(f"model: {MODEL}")

load_model()
ip, session = who_is_calling()
mode = st.radio("Mode", ["Single", "Bulk (ZIP)"], horizontal=True)

if mode == "Single":
    file = st.file_uploader("Upload an image", type=list(IMG_TYPES))
    if file and st.button("Caption it"):
        M["requests"].labels("single", ip, session).inc()
        with M["active"].labels("single").track_inprogress(), \
                M["request_time"].labels("single").time():
            text = caption(file.getvalue(), "single")
        st.image(file, width=400)
        if text:
            st.success(text)
        else:
            st.error("Couldn't read that image.")

else:
    file = st.file_uploader("Upload a ZIP of images", type=["zip"])
    if file and st.button("Caption all"):
        M["requests"].labels("bulk", ip, session).inc()
        with M["active"].labels("bulk").track_inprogress(), \
                M["request_time"].labels("bulk").time():
            try:
                zf = zipfile.ZipFile(io.BytesIO(file.getvalue()))
            except zipfile.BadZipFile:
                M["errors"].labels("bulk", "BadZipFile").inc()
                st.error("That isn't a valid zip file.")
                st.stop()

            names = images_in_zip(zf)
            rows, left = [], len(names)
            M["queue"].inc(left)
            bar = st.progress(0.0)
            try:
                for i, name in enumerate(names):
                    text = caption(zf.read(name), "bulk")
                    rows.append({"file": name, "caption": text or "(couldn't read image)"})
                    M["queue"].dec()
                    left -= 1
                    bar.progress((i + 1) / len(names), text=f"{i + 1}/{len(names)}")
            finally:
                M["queue"].dec(left)  # if the user stops halfway

        if rows:
            df = pd.DataFrame(rows)
            st.dataframe(df, hide_index=True)
            st.download_button("Download CSV", df.to_csv(index=False), "captions.csv")
        else:
            st.warning("No images found in the zip.")
