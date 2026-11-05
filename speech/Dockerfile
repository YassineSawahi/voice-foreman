# Speech service: faster-whisper (STT), Kokoro (TTS) and Silero VAD, on the GPU.
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

COPY requirements.txt /tmp/requirements.txt
# faster-whisper and kokoro-onnx depend on the CPU onnxruntime; replace it with the
# GPU build, which provides the same module. 1.26.0 is the last onnxruntime-gpu built
# for CUDA 12 (1.28+ needs CUDA 13); ctranslate2 needs CUDA 12, so both share this image.
RUN pip install -r /tmp/requirements.txt \
 && pip uninstall -y onnxruntime \
 && pip install --force-reinstall --no-deps "onnxruntime-gpu==1.26.0"

ADD https://github.com/snakers4/silero-vad/raw/v5.1.2/src/silero_vad/data/silero_vad.onnx /models/silero_vad.onnx

COPY server.py /app/server.py
WORKDIR /app
EXPOSE 8765
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8765"]
